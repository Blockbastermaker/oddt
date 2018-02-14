from __future__ import print_function
import sys
from os.path import dirname, isfile, join as path_join
from functools import partial
import json

import numpy as np
from scipy.stats import pearsonr
from sklearn.metrics import r2_score

from sklearn.linear_model import SGDRegressor
from sklearn.ensemble import RandomForestRegressor
from sklearn.neural_network import MLPRegressor

from oddt.metrics import rmse
from oddt.scoring import scorer
from oddt.fingerprints import PLEC
from oddt.scoring.descriptors import universal_descriptor


class PLECscore(scorer):
    def __init__(self, protein=None, n_jobs=-1, version='linear',
                 depth_protein=5, depth_ligand=1, size=65536):
        self.protein = protein
        self.n_jobs = n_jobs
        self.version = version
        self.depth_protein = depth_protein
        self.depth_ligand = depth_ligand
        self.size = size

        plec_func = partial(PLEC,
                            depth_ligand=depth_ligand,
                            depth_protein=depth_protein,
                            size=size,
                            count_bits=True,
                            sparse=True,
                            ignore_hoh=True)
        descriptors = universal_descriptor(plec_func, protein=protein,
                                           shape=size, sparse=True)

        if version == 'linear':
            model = SGDRegressor(fit_intercept=False,
                                 loss='huber',
                                 penalty='elasticnet',
                                 random_state=0,
                                 verbose=0,
                                 n_iter=100,
                                 alpha=1e-4,
                                 epsilon=1e-1)
        elif version == 'nn':
            model = MLPRegressor((200, 200, 200),
                                 batch_size=10,
                                 random_state=0,
                                 verbose=0,
                                 solver='lbfgs')
        elif version == 'rf':
            model = RandomForestRegressor(n_estimators=100,
                                          n_jobs=-1,
                                          verbose=0,
                                          oob_score=True,
                                          random_state=0)
        else:
            raise ValueError('The version "%s" is not supported by PLECscore'
                             % version)

        super(PLECscore, self).__init__(model, descriptors,
                                        score_title='PLEC%s_p%i_l%i' %
                                        (version, depth_protein, depth_ligand))

    def gen_training_data(self,
                          pdbbind_dir,
                          pdbbind_versions=(2016,),
                          home_dir=None):
        if home_dir is None:
            home_dir = path_join(dirname(__file__), 'PLECscore')
        filename = path_join(home_dir, 'plecscore_descs_p%i_l%i_s%i.csv' %
                             (self.depth_protein, self.depth_ligand, self.size))

        super(PLECscore, self)._gen_pdbbind_desc(
            pdbbind_dir=pdbbind_dir,
            pdbbind_versions=pdbbind_versions,
            desc_path=filename,
            include_general_set=True,
            use_proteins=True,
            sparse=True,
        )

    def gen_json(self, home_dir=None, pdbbind_version=2016):
        self.train(home_dir=home_dir)
        if isinstance(self.model, SGDRegressor):
            attributes = ['coef_', 'intercept_']
        elif isinstance(self.model, MLPRegressor):
            attributes = ['loss_', 'coefs_', 'intercepts_', 'n_iter_',
                          'n_layers_', 'n_outputs_', 'out_activation_']

        out = {}
        for attr_name in attributes:
            attr = getattr(self.model, attr_name)
            # convert numpy arrays to list for json
            if isinstance(attr, np.array):
                attr = attr.tolist()
            elif (isinstance(attr, (list, tuple)) and
                  isinstance(attr[0], np.array)):
                attr = [x.tolist() for x in attr]
            out[attr_name] = attr

        json_path = path_join(home_dir, 'plecscore_%s_p%i_l%i_s%i_pdbbind%i.json' %
                              (self.version, self.depth_protein,
                               self.depth_ligand, self.size, pdbbind_version))

        with open(json_path, 'w') as json_f:
            json.dump(out, json_f, indent=2)
        return json_path

    def train(self, home_dir=None, sf_pickle=None, pdbbind_version=2016,
              ignore_json=False):
        if not home_dir:
            home_dir = path_join(dirname(__file__), 'PLECscore')
        desc_path = path_join(home_dir, 'plecscore_descs_p%i_l%i_s%i.csv' %
                              (self.depth_protein, self.depth_ligand, self.size))

        json_path = path_join(
            home_dir, 'plecscore_%s_p%i_l%i_s%i_pdbbind%i.json' %
            (self.version, self.depth_protein,
             self.depth_ligand, self.size, pdbbind_version))

        if (self.version in ['linear'] and  # TODO: support other models
                isfile(json_path) and
                not ignore_json):
            print('Loading pretrained PLECscore %s with depths P%i L%i on '
                  'PDBBind v%i'
                  % (self.version, self.depth_protein, self.depth_ligand,
                     pdbbind_version), file=sys.stderr)
            with open(json_path) as json_f:
                json_data = json.load(json_f)
            for k, v in json_data.items():
                if isinstance(v, list):
                    if isinstance(v[0], list):
                        v = [np.array(x) for x in v]
                    else:
                        v = np.array(v)
                setattr(self.model, k, v)
        else:
            super(PLECscore, self)._load_pdbbind_desc(
                desc_path,
                train_set=('general', 'refined'),
                pdbbind_version=pdbbind_version)

            print('Training PLECscore %s with depths P%i L%i on PDBBind v%i'
                  % (self.version, self.depth_protein, self.depth_ligand,
                     pdbbind_version), file=sys.stderr)

            # FIXME: RF does not like CSR matrix in OOB pred for older sklearn
            if self.version == 'rf':
                self.model.fit(self.train_descs.toarray(), self.train_target)
            else:
                self.model.fit(self.train_descs, self.train_target)

            sets = [
                ('Test', self.model.predict(self.test_descs), self.test_target),
                ('Train', self.model.predict(self.train_descs), self.train_target)]
            if self.version == 'rf':
                sets.append(('OOB', self.model.oob_prediction_, self.train_target))

            for name, pred, target in sets:
                print('%s set:' % name,
                      'R2_score: %.4f' % r2_score(target, pred),
                      'Rp: %.4f' % pearsonr(target, pred)[0],
                      'RMSE: %.4f' % rmse(target, pred),
                      sep='\t', file=sys.stderr)

            if sf_pickle is None:
                return self.save('PLEC%s_p%i_l%i_pdbbind%i_s%i.pickle'
                                 % (self.version, self.depth_protein,
                                    self.depth_ligand, pdbbind_version, self.size))
            else:
                return self.save(sf_pickle)

    @classmethod
    def load(self, filename=None, version='linear', pdbbind_version=2016):
        if filename is None:
            # FIXME: it would be cool to have templates of names for a class
            fname = ('PLEC%s_p%i_l%i_pdbbind%i_s%i.pickle' %
                     (self.version, self.depth_protein, self.depth_ligand,
                      pdbbind_version, self.size))
            for f in [fname, path_join(dirname(__file__), fname)]:
                if isfile(f):
                    filename = f
                    break
            else:
                print('No pickle, training new scoring function.',
                      file=sys.stderr)
                sf = PLECscore(version=version)
                filename = sf.train(sf_pickle=filename,
                                    pdbbind_version=pdbbind_version)
        return scorer.load(filename)