# Sebastian Raschka 2014-2020
# mlxtend Machine Learning Library Extensions
#
# Algorithm for sequential feature selection.
# Author: Sebastian Raschka <sebastianraschka.com>
#
# License: BSD 3 clause

# Modified by Jonathan Taylor 2021
#
# Derives from sequential_feature_selector
# but allows custom model search

import types
import sys
from functools import partial
from copy import deepcopy
from itertools import combinations, chain

import numpy as np
import scipy as sp

from sklearn.metrics import get_scorer
from sklearn.base import (clone, MetaEstimatorMixin)
from sklearn.utils import check_random_state
from sklearn.model_selection import cross_val_score
from joblib import Parallel, delayed

from ..externals.name_estimators import _name_estimators
from ..utils.base_compostion import _BaseXComposition
from .columns import (_get_column_info,
                     Column,
                     _categorical_from_df,
                     _check_categories)

def _calc_score(selector,
                X,
                y,
                state,
                groups=None,
                **fit_params):
    
    X_state = selector.build_submodel(X, state)
    
    if selector.cv:
        scores = cross_val_score(selector.est_,
                                 X_state,
                                 y,
                                 groups=groups,
                                 cv=selector.cv,
                                 scoring=selector.scorer,
                                 n_jobs=1,
                                 pre_dispatch=selector.pre_dispatch,
                                 fit_params=fit_params)
    else:
        selector.est_.fit(X_state,
                          y,
                          **fit_params)
        scores = np.array([selector.scorer(selector.est_,
                                           X_state,
                                           y)])
    return state, scores


class FeatureSelector(_BaseXComposition, MetaEstimatorMixin):

    """Feature Selection for Classification and Regression.

    Parameters
    ----------
    estimator : scikit-learn classifier or regressor
    initial_state : object
        Initial state of feature selector.
    state_generator : callable
        Callable taking single argument `state` and returning
        candidates for next batch of scores to be calculated.
    build_submodel : callable
        Callable taking two arguments `(X, state)` that returns
        model matrix represented by `state`.
    check_finished : callable
        Callable taking three arguments 
        `(results, best_state, batch_results)` which determines if
        the state generator should step. Often will just check
        if there is a better score than that at current best state
        but can use entire set of results if desired.
    verbose : int (default: 0), level of verbosity to use in logging.
        If 0, no output,
        if 1 number of features in current set, if 2 detailed logging 
        including timestamp and cv scores at step.
    scoring : str, callable, or None (default: None)
        If None (default), uses 'accuracy' for sklearn classifiers
        and 'r2' for sklearn regressors.
        If str, uses a sklearn scoring metric string identifier, for example
        {accuracy, f1, precision, recall, roc_auc} for classifiers,
        {'mean_absolute_error', 'mean_squared_error'/'neg_mean_squared_error',
        'median_absolute_error', 'r2'} for regressors.
        If a callable object or function is provided, it has to be conform with
        sklearn's signature ``scorer(estimator, X, y)``; see
        http://scikit-learn.org/stable/modules/generated/sklearn.metrics.make_scorer.html
        for more information.
    cv : int (default: 5)
        Integer or iterable yielding train, test splits. If cv is an integer
        and `estimator` is a classifier (or y consists of integer class
        labels) stratified k-fold. Otherwise regular k-fold cross-validation
        is performed. No cross-validation if cv is None, False, or 0.
    n_jobs : int (default: 1)
        The number of CPUs to use for evaluating different feature subsets
        in parallel. -1 means 'all CPUs'.
    pre_dispatch : int, or string (default: '2*n_jobs')
        Controls the number of jobs that get dispatched
        during parallel execution if `n_jobs > 1` or `n_jobs=-1`.
        Reducing this number can be useful to avoid an explosion of
        memory consumption when more jobs get dispatched than CPUs can process.
        This parameter can be:
        None, in which case all the jobs are immediately created and spawned.
            Use this for lightweight and fast-running jobs,
            to avoid delays due to on-demand spawning of the jobs
        An int, giving the exact number of total jobs that are spawned
        A string, giving an expression as a function
            of n_jobs, as in `2*n_jobs`
    clone_estimator : bool (default: True)
        Clones estimator if True; works with the original estimator instance
        if False. Set to False if the estimator doesn't
        implement scikit-learn's set_params and get_params methods.
        In addition, it is required to set cv=0, and n_jobs=1.

    Attributes
    ----------
    results_ : dict
        A dictionary of selected feature subsets during the
        selection, where the dictionary keys are
        the states of these feature selector. The dictionary
        values are dictionaries themselves with the following
        keys: 'cv_scores' (list individual cross-validation scores)
              'avg_score' (average cross-validation score)

    Examples
    -----------
    For usage examples, please see
    TBD

    """
    def __init__(self,
                 estimator,
                 initial_state,
                 state_generator,
                 build_submodel,
                 check_finished,
                 verbose=0,
                 scoring=None,
                 cv=5,
                 n_jobs=1,
                 pre_dispatch='2*n_jobs',
                 clone_estimator=True,
                 fixed_features=None):

        self.estimator = estimator
        self.initial_state = initial_state
        self.state_generator = state_generator
        self.build_submodel = build_submodel
        self.check_finished = check_finished
        self.pre_dispatch = pre_dispatch
        # Want to raise meaningful error message if a
        # cross-validation generator is inputted
        if isinstance(cv, types.GeneratorType):
            err_msg = ('Input cv is a generator object, which is not '
                       'supported. Instead please input an iterable yielding '
                       'train, test splits. This can usually be done by '
                       'passing a cross-validation generator to the '
                       'built-in list function. I.e. cv=list(<cv-generator>)')
            raise TypeError(err_msg)
        self.cv = cv
        self.n_jobs = n_jobs
        self.verbose = verbose
        self.clone_estimator = clone_estimator

        if self.clone_estimator:
            self.est_ = clone(self.estimator)
        else:
            self.est_ = self.estimator
        self.scoring = scoring

        if scoring is None:
            if self.est_._estimator_type == 'classifier':
                scoring = 'accuracy'
            elif self.est_._estimator_type == 'regressor':
                scoring = 'r2'
            else:
                raise AttributeError('Estimator must '
                                     'be a Classifier or Regressor.')
        if isinstance(scoring, str):
            self.scorer = get_scorer(scoring)
        else:
            self.scorer = scoring

        self.fitted = False
        self.results_ = {}
        self.interrupted_ = False

        # don't mess with this unless testing
        self._TESTING_INTERRUPT_MODE = False

    @property
    def named_estimators(self):
        """
        Returns
        -------
        List of named estimator tuples, like [('svc', SVC(...))]
        """
        return _name_estimators([self.estimator])

    def get_params(self, deep=True):
        #
        # Return estimator parameter names for GridSearch support.
        #
        return self._get_params('named_estimators', deep=deep)

    def set_params(self, **params):
        """Set the parameters of this estimator.
        Valid parameter keys can be listed with ``get_params()``.

        Returns
        -------
        self
        """
        self._set_params('estimator', 'named_estimators', **params)
        return self

    def fit(self, X, y, custom_feature_names=None, groups=None, **fit_params):
        """Perform feature selection and learn model from training data.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples, n_features]
            Training vectors, where n_samples is the number of samples and
            n_features is the number of features.
            New in v 0.13.0: pandas DataFrames are now also accepted as
            argument for X.
        y : array-like, shape = [n_samples]
            Target values.
            New in v 0.13.0: pandas DataFrames are now also accepted as
            argument for y.
        custom_feature_names : None or tuple (default: tuple)
            Custom feature names for `self.k_feature_names` and
            `self.subsets_[i]['feature_names']`.
            (new in v 0.13.0)
        groups : array-like, with shape (n_samples,), optional
            Group labels for the samples used while splitting the dataset into
            train/test set. Passed to the fit method of the cross-validator.
        fit_params : various, optional
            Additional parameters that are being passed to the estimator.
            For example, `sample_weights=weights`.

        Returns
        -------
        self : object

        """

        # reset from a potential previous fit run
        self.results_ = {}
        self.finished = False
        self.interrupted_ = False

        # fit initial model

        _state = self.initial_state

        _state, _scores = _calc_score(self,
                                      X,
                                      y,
                                      _state,
                                      groups=groups,
                                      **fit_params)

        # keep a running track of the best state

        self.best_state_ = _state
        self.best_score_ = np.nanmean(_scores)

        self.update_results_check({_state: {'cv_scores': _scores,
                                            'avg_score': np.nanmean(_scores)}})
                
        try:
            while not self.finished:

                batch_results = self._batch(_state,
                                            X,
                                            y,
                                            groups=groups,
                                            **fit_params)

                _state, _score, self.finished = self.update_results_check(batch_results)
                print(_state, self.finished, _score, self.best_score_)
                                           
                if self._TESTING_INTERRUPT_MODE:
                    raise KeyboardInterrupt

        except KeyboardInterrupt:
            self.interrupted_ = True
            sys.stderr.write('\nSTOPPING EARLY DUE TO KEYBOARD INTERRUPT...')

        self.fitted = True
        self.postprocess()
        return self

    def _batch(self,
               state,
               X,
               y,
               groups=None,
               **fit_params):

        results = {}

        candidates = self.state_generator(state)

        if candidates is not None:

            parallel = Parallel(n_jobs=self.n_jobs,
                                verbose=self.verbose,
                                pre_dispatch=self.pre_dispatch)

            work = parallel(delayed(_calc_score)
                            (self,
                             X,
                             y,
                             state,
                             groups=groups,
                             **fit_params)
                            for state in candidates)

            for state, cv_scores in work:
                results[state] = {'cv_scores': cv_scores,
                                  'avg_score': np.nanmean(cv_scores)}

        return results

    def transform(self, X):
        """Reduce X to its most important features.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples, n_features]
            Training vectors, where n_samples is the number of samples and
            n_features is the number of features.
            New in v 0.13.0: pandas DataFrames are now also accepted as
            argument for X.

        Returns
        -------
        Reduced feature subset of X, shape={n_samples, k_features}

        """
        self._check_fitted()
        return self.build_submodel(X, self.best_state_)

    def fit_transform(self,
                      X,
                      y,
                      groups=None,
                      **fit_params):
        """Fit to training data then reduce X to its most important features.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples, n_features]
            Training vectors, where n_samples is the number of samples and
            n_features is the number of features.
            New in v 0.13.0: pandas DataFrames are now also accepted as
            argument for X.
        y : array-like, shape = [n_samples]
            Target values.
            New in v 0.13.0: a pandas Series are now also accepted as
            argument for y.
        groups : array-like, with shape (n_samples,), optional
            Group labels for the samples used while splitting the dataset into
            train/test set. Passed to the fit method of the cross-validator.
        fit_params : various, optional
            Additional parameters that are being passed to the estimator.
            For example, `sample_weights=weights`.

        Returns
        -------
        Reduced feature subset of X, shape={n_samples, k_features}

        """
        self.fit(X, y, groups=groups, **fit_params)
        return self.transform(X)

    def get_metric_dict(self, confidence_interval=0.95):
        """Return metric dictionary

        Parameters
        ----------
        confidence_interval : float (default: 0.95)
            A positive float between 0.0 and 1.0 to compute the confidence
            interval bounds of the CV score averages.

        Returns
        ----------
        Dictionary with items where each dictionary value is a list
        with the number of iterations (number of feature subsets) as
        its length. The dictionary keys corresponding to these lists
        are as follows:
            'state': tuple of the indices of the feature subset
            'cv_scores': list with individual CV scores
            'avg_score': of CV average scores
            'std_dev': standard deviation of the CV score average
            'std_err': standard error of the CV score average
            'ci_bound': confidence interval bound of the CV score average

        """
        self._check_fitted()
        fdict = deepcopy(self.results_)

        def _calc_confidence(ary, confidence=0.95):
            std_err = sp.stats.sem(ary)
            bound = std_err * sp.stats.t._ppf((1 + confidence) / 2.0, len(ary))
            return bound, std_err

        for k in fdict:
            std_dev = np.std(self.results_[k]['cv_scores'])
            bound, std_err = self._calc_confidence(
                self.results_[k]['cv_scores'],
                confidence=confidence_interval)
            fdict[k]['ci_bound'] = bound
            fdict[k]['std_dev'] = std_dev
            fdict[k]['std_err'] = std_err
        return fdict

    def _check_fitted(self):
        if not self.fitted:
            raise AttributeError('{} has not been fitted yet.'.format(self.__class__))

    def update_results_check(self,
                             batch_results):
        """
        Update `self.results_` with current batch
        and return a boolean about whether 
        we should continue or not.

        Parameters
        ----------

        batch_results : dict
            Dictionary of results from a batch fit.
            Keys are the state with values
            dictionaries having keys
            `cv_scores`, `avg_scores`.

        Returns
        -------

        best_state : object
            State that had the best `avg_score`

        fitted : bool
            If batch_results is empty, fitting
            has terminated so return True.
            Otherwise False.

        """

        finished = batch_results == {}

        if not finished:
            self.results_.update(batch_results)

            (cur_state,
             cur_score,
             finished) = self.check_finished(self.results_,
                                             self.best_state_,
                                             batch_results)
            if cur_score > self.best_score_:
                self.best_state_ = cur_state
                self.best_score_ = cur_score
            return cur_state, cur_score, finished
        else:
            return None, None, True

    def postprocess(self):
        """
        Find the best model and score from `self.results_`.
        """

        self.best_state_ = None
        self.best_score_ = -np.inf

        for state, result in self.results_.items():
            if result['avg_score'] > self.best_score_:
                self.best_state_ = state
                self.best_score_ = result['avg_score']
        
class MinMaxCandidates(object):

    def __init__(self,
                 X,
                 min_features=1,
                 max_features=1,
                 fixed_features=None,
                 custom_feature_names=None,
                 categorical_features=None):
        """
        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples, n_features]
            Training vectors, where n_samples is the number of samples and
            n_features is the number of features.
            New in v 0.13.0: pandas DataFrames are now also accepted as
            argument for X.
        min_features : int (default: 1)
            Minumum number of features to select
        max_features : int (default: 1)
            Maximum number of features to select
        fixed_features : column identifiers, default=None
            Subset of features to keep. Stored as `self.columns[fixed_features]`
            where `self.columns` will correspond to columns if X is a `pd.DataFrame`
            or an array of integers if X is an `np.ndarray`
        custom_feature_names : None or tuple (default: tuple)
                Custom feature names for `self.k_feature_names` and
                `self.subsets_[i]['feature_names']`.
                (new in v 0.13.0)
        categorical_features : array-like of {bool, int} of shape (n_features) 
                or shape (n_categorical_features,), default=None.
            Indicates the categorical features.

            - None : no feature will be considered categorical.
            - boolean array-like : boolean mask indicating categorical features.
            - integer array-like : integer indices indicating categorical
              features.

            For each categorical feature, there must be at most `max_bins` unique
            categories, and each categorical value must be in [0, max_bins -1].

        """

        if hasattr(X, 'loc'):
            X_ = X.values
            is_categorical, is_ordinal = _categorical_from_df(X)
            self.columns = X.columns
        else:
            X_ = X
            is_categorical = _check_categories(categorical_features,
                                               X_)[0]
            if is_categorical is None:
                is_categorical = np.zeros(X_.shape[1], np.bool)
            is_ordinal = np.zeros_like(is_categorical)
            self.columns = np.arange(X.shape[1])

        nfeatures = X_.shape[0]

        if (not isinstance(max_features, int) or
                (max_features > nfeatures or max_features < 1)):
            raise AttributeError('max_features must be'
                                 ' smaller than %d and larger than 0' %
                                 (nfeatures + 1))

        if (not isinstance(min_features, int) or
                (min_features > nfeatures or min_features < 1)):
            raise AttributeError('min_features must be'
                                 ' smaller than %d and larger than 0'
                                 % (nfeatures + 1))

        if max_features < min_features:
            raise AttributeError('min_features must be <= max_features')

        self.min_features, self.max_features = min_features, max_features

        # make a mapping from the column info to columns in
        # implied design matrix

        self.column_info_ = _get_column_info(X,
                                             range(X.shape[1]),
                                             is_categorical,
                                             is_ordinal)
        self.column_map_ = {}
        idx = 0
        for col in self.columns:
            l = self.column_info_[col].columns
            self.column_map_[col] = range(idx, idx +
                                          len(l))
            idx += len(l)
        if (custom_feature_names is not None
                and len(custom_feature_names) != nfeatures):
            raise ValueError('If custom_feature_names is not None, '
                             'the number of elements in custom_feature_names '
                             'must equal %d the number of columns in X.' % idx)
        if custom_feature_names is not None:
            # recompute the Column info using custom_feature_names
            for i, col in enumerate(self.columns):
                cur_col = self.column_info_[col]
                new_name = custom_feature_names[i]
                old_name = cur_col.name
                self.column_info_[col] = Column(col,
                                                new_name,
                                                col.is_categorical,
                                                col.is_ordinal,
                                                tuple([n.replace(old_name,
                                                                 new_name) for n in col.columns]),
                                                col.encoder)

        self._have_already_run = False
        if fixed_features is not None:
            self.fixed_features = set([self.columns[f] for f in fixed_features])
        else:
            self.fixed_features = set([])
            
    def __call__(self, state):
        """
        Produce candidates for fitting.

        Parameters
        ----------

        state : ignored

        Returns
        -------
        candidates : iterator
            A generator of (indices, label) where indices
            are columns of X and label is a name for the 
            given model. The iterator cycles through
            all combinations of columns of nfeature total
            of size ranging between min_features and max_features.
            If appropriate, restricts combinations to include
            a set of fixed features.
            Models are labeled with a tuple of the feature names.
            The names of the columns default to strings of integers
            from range(nfeatures).

        """

        if not self._have_already_run:
            self._have_already_run = True # maybe could be done with a StopIteration on candidates?
            def chain_(i):
                return (c for c in combinations(self.columns, r=i)
                        if self.fixed_features.issubset(c))
            
            candidates = chain.from_iterable(chain_(i) for i in
                                             range(self.min_features,
                                                   self.max_features+1))
            return candidates
        
    def check_finished(self,
                       results,
                       best_state,
                       batch_results):
        """
        Check if we should continue or not. 
        For exhaustive search we stop because
        all models are fit in a single batch.
        """
        return best_state, results[best_state]['avg_score'], True

class StepCandidates(MinMaxCandidates):

    def __init__(self,
                 X,
                 direction='forward',
                 min_features=1,
                 max_features=1,
                 fixed_features=None,
                 custom_feature_names=None,
                 categorical_features=None):
        """
        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples, n_features]
            Training vectors, where n_samples is the number of samples and
            n_features is the number of features.
            New in v 0.13.0: pandas DataFrames are now also accepted as
            argument for X.
        direction : str
            One of ['forward', 'backward', 'both']
        min_features : int (default: 1)
            Minumum number of features to select
        max_features : int (default: 1)
            Maximum number of features to select
        fixed_features : column identifiers, default=None
            Subset of features to keep. Stored as `self.columns[fixed_features]`
            where `self.columns` will correspond to columns if X is a `pd.DataFrame`
            or an array of integers if X is an `np.ndarray`
        custom_feature_names : None or tuple (default: tuple)
                Custom feature names for `self.k_feature_names` and
                `self.subsets_[i]['feature_names']`.
                (new in v 0.13.0)
        categorical_features : array-like of {bool, int} of shape (n_features) 
                or shape (n_categorical_features,), default=None.
            Indicates the categorical features.

            - None : no feature will be considered categorical.
            - boolean array-like : boolean mask indicating categorical features.
            - integer array-like : integer indices indicating categorical
              features.

            For each categorical feature, there must be at most `max_bins` unique
            categories, and each categorical value must be in [0, max_bins -1].

        """

        self.direction = direction
        MinMaxCandidates.__init__(self,
                                  X,
                                  min_features,
                                  max_features,
                                  fixed_features,
                                  custom_feature_names,
                                  categorical_features)
            
    def __call__(self, state):
        """
        Produce candidates for fitting.
        For stepwise search this depends on the direction.

        If 'forward', all columns not in the current state
        are added (maintaining an upper limit on the number of columns 
        at `self.max_features`).

        If 'backward', all columns not in the current state
        are dropped (maintaining a lower limit on the number of columns 
        at `self.min_features`).

        All candidates include `self.fixed_features` if any.
        
        Parameters
        ----------

        state : ignored

        Returns
        -------
        candidates : iterator
            A generator of (indices, label) where indices
            are columns of X and label is a name for the 
            given model. The iterator cycles through
            all combinations of columns of nfeature total
            of size ranging between min_features and max_features.
            If appropriate, restricts combinations to include
            a set of fixed features.
            Models are labeled with a tuple of the feature names.
            The names of the columns default to strings of integers
            from range(nfeatures).

        """

        state = set(state)
        if len(state) < self.max_features: # union
            forward = (tuple(sorted(state | set([c]))) for c in self.columns if (c not in state and
                                                                self.fixed_features.issubset(state | set([c]))))
        else:
            forward = None
        if len(state) > self.min_features: # symmetric difference
            backward = (tuple(sorted(state ^ set([c]))) for c in self.columns if (c in state and
                                                                 self.fixed_features.issubset(state ^ set([c]))))
        else:
            backward = None

        if self.direction == 'forward':
            return forward
        elif self.direction == 'backward':
            return backward
        else:
            return chain.from_iterable([forward, backward])
        
    def check_finished(self,
                       results,
                       best_state,
                       batch_results):
        """
        Check if we should continue or not. 

        For stepwise search we stop if we cannot improve
        over our current best score.

        """
        batch_best_score = -np.inf
        batch_best_state = None

        for state in batch_results:
            if batch_results[state]['avg_score'] > batch_best_score:
                batch_best_score = batch_results[state]['avg_score']
                batch_best_state = state
                
        finished = batch_best_score <= results[best_state]['avg_score']
        print(batch_best_state, batch_best_score, results[best_state]['avg_score'], finished, 'BEST!!!!!!!!!!!!!!!!!!!!!!!')
        return batch_best_state, batch_best_score, finished

    
def min_max_candidates(X,
                       min_features=1,
                       max_features=1,
                       fixed_features=None,
                       custom_feature_names=None,
                       categorical_features=None):
    """
    Parameters
    ----------
    X : {array-like, sparse matrix}, shape = [n_samples, n_features]
        Training vectors, where n_samples is the number of samples and
        n_features is the number of features.
        New in v 0.13.0: pandas DataFrames are now also accepted as
        argument for X.
    min_features : int (default: 1)
        Minumum number of features to select
    max_features : int (default: 1)
        Maximum number of features to select
    fixed_features : column identifiers, default=None
        Subset of features to keep. Stored as `self.columns[fixed_features]`
        where `self.columns` will correspond to columns if X is a `pd.DataFrame`
        or an array of integers if X is an `np.ndarray`
    custom_feature_names : None or tuple (default: tuple)
            Custom feature names for `self.k_feature_names` and
            `self.subsets_[i]['feature_names']`.
            (new in v 0.13.0)
    categorical_features : array-like of {bool, int} of shape (n_features) 
            or shape (n_categorical_features,), default=None.
        Indicates the categorical features.

        - None : no feature will be considered categorical.
        - boolean array-like : boolean mask indicating categorical features.
        - integer array-like : integer indices indicating categorical
          features.

        For each categorical feature, there must be at most `max_bins` unique
        categories, and each categorical value must be in [0, max_bins -1].

    Returns
    -------

    initial_state : tuple
        (column_names, feature_idx)

    state_generator : callable
        Object that proposes candidates
        based on current state. Takes a single 
        argument `state`

    build_submodel : callable
        Candidate generator that enumerate
        all valid subsets of columns.

    check_finished : callable
        Check whether to stop. Takes two arguments:
        `best_result` a dict with keys ['cv_scores', 'avg_score'];
        and `state`.

    """

    min_max = MinMaxCandidates(X,
                               min_features,
                               max_features,
                               fixed_features,
                               custom_feature_names,
                               categorical_features)
    
    # if any categorical features or an intercept
    # is included then we must
    # create a new design matrix

    def build_submodel(column_info, X, cols):
        return np.column_stack([column_info[col].get_columns(X, fit=True)[0] for col in cols])
    build_submodel = partial(build_submodel, min_max.column_info_)

    if min_max.fixed_features:
        initial_features = sorted(min_max.fixed_features)
    else:
        initial_features = range(min_max.min_features)
    initial_state = tuple(initial_features)
    return initial_state, min_max, build_submodel, min_max.check_finished

def step_candidates(X,
                    direction='forward',
                    min_features=1,
                    max_features=1,
                    random_state=0,
                    fixed_features=None,
                    initial_features=None,
                    custom_feature_names=None,
                    categorical_features=None):
    """
    Parameters
    ----------
    X : {array-like, sparse matrix}, shape = [n_samples, n_features]
        Training vectors, where n_samples is the number of samples and
        n_features is the number of features.
        New in v 0.13.0: pandas DataFrames are now also accepted as
        argument for X.
    direction : str
        One of ['forward', 'backward', 'both']
    min_features : int (default: 1)
        Minumum number of features to select
    max_features : int (default: 1)
        Maximum number of features to select
    fixed_features : column identifiers, default=None
        Subset of features to keep. Stored as `self.columns[fixed_features]`
        where `self.columns` will correspond to columns if X is a `pd.DataFrame`
        or an array of integers if X is an `np.ndarray`
    initial_features : column identifiers, default=None
        Subset of features to be used to initialize when direction
        is `both`. If None defaults to behavior of `forward`.
        where `self.columns` will correspond to columns if X is a `pd.DataFrame`
        or an array of integers if X is an `np.ndarray`
    custom_feature_names : None or tuple (default: tuple)
            Custom feature names for `self.k_feature_names` and
            `self.subsets_[i]['feature_names']`.
            (new in v 0.13.0)
    categorical_features : array-like of {bool, int} of shape (n_features) 
            or shape (n_categorical_features,), default=None.
        Indicates the categorical features.

        - None : no feature will be considered categorical.
        - boolean array-like : boolean mask indicating categorical features.
        - integer array-like : integer indices indicating categorical
          features.

        For each categorical feature, there must be at most `max_bins` unique
        categories, and each categorical value must be in [0, max_bins -1].

    Returns
    -------

    initial_state : tuple
        (column_names, feature_idx)

    state_generator : callable
        Object that proposes candidates
        based on current state. Takes a single 
        argument `state`

    build_submodel : callable
        Candidate generator that enumerate
        all valid subsets of columns.

    check_finished : callable
        Check whether to stop. Takes two arguments:
        `best_result` a dict with keys ['cv_scores', 'avg_score'];
        and `state`.

    """

    step = StepCandidates(X,
                          direction,
                          min_features,
                          max_features,
                          fixed_features,
                          custom_feature_names,
                          categorical_features)
    
    # if any categorical features or an intercept
    # is included then we must
    # create a new design matrix

    def build_submodel(column_info, X, cols):
        return np.column_stack([column_info[col].get_columns(X, fit=True)[0] for col in cols])
    build_submodel = partial(build_submodel, step.column_info_)

    if direction in ['forward', 'both']:
        if step.fixed_features:
            forward_features = sorted(step.fixed_features)
        else:
            forward_features = range(step.min_features)
        if direction == 'forward':
            initial_features = forward_features
        else:
            if initial_features is None:
                initial_features = forward_features
    elif direction == 'backward':
        if initial_features is None:
            random_state = check_random_state(random_state)
            initial_features = sorted(random_state.choice([col for col in step.column_info_],
                                                          step.max_features,
                                                          replace=False))
    initial_state = tuple(initial_features)

    if len(initial_features) > step.max_features:
        raise ValueError('initial_features should be of length <= %d' % step.max_features)
    if len(initial_features) < step.min_features:
        raise ValueError('initial_features should be of length >= %d' % step.min_features)
    if not step.fixed_features.issubset(initial_features):
        raise ValueError('initial_features should contain %s' % str(step.fixed_features))

    return initial_state, step, build_submodel, step.check_finished


    
