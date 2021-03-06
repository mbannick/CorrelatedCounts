# -*- coding: utf-8 -*-
"""
    core
    ~~~~

    Core module for correlated count.
"""
import logging
import numpy as np
from copy import deepcopy

from ccount import optimization
from ccount import utils
from ccount.bsplines import spline_design_mat

LOG = logging.getLogger(__name__)


class CorrelatedModel:
    """Correlated model with multiple outcomes.

    Attributes
    ----------
    m : int
        Number of individuals.
    n : int
        Number of outcomes.
    l : int
        Number of parameters in the considered distribution.
    d : array_like
        Number of covariates for each parameter and outcome.
    Y : array_like
        Observations.
    X : :obj: `list` of :obj: `list` of :obj: `numpy.ndarray`
        List of list of 2D arrays, storing the covariates for each parameter
        and outcome.
    g : :obj: `list` of :obj: `function`
        List of inverse link functions for each parameter.
    f : function
        Log likelihood function, better be `numpy.ufunc`.
        Needs to return an an array in the same shape as Y
    beta : :obj: `list` of :obj: `list` of :obj: `numpy.ndarray`
        Fixed effects for predicting the parameters.
    U : array_like
        Random effects for predicting the parameters. Assume random effects
        follow multi-normal distribution.
    D : array_like
        Covariance matrix for the random effects distribution.
    P : array_like
        Parameters for each individual and outcome.

    """

    def __init__(self, m, n, l, d, Y, X, g, f,
                 spline_specs=None, group_id=None, offset=None, weights=None, add_intercepts=False, normalize_X=True):
        """Correlated Model initialization method.

        Parameters
        ----------
        m : int
            Number of individuals.
        n : int
            Number of outcomes.
        l : int
            Number of parameters in the considered distribution.
        d : array_like
            Number of covariates for each parameter and outcome.
        Y : array_like
            Observations.
        X : :obj: `list` of :obj: `list` of :obj: `numpy.ndarray`
            List of list of 2D arrays, storing the covariates for each parameter
            and outcome. (no intercept -- intercept automatically added)
        spline_specs : :obj: `list` of :obj: `list` of :obj: `list` of `dict`
            List of list of arrays, storing the design matrix for a covariate to put a b-spline on for each parameter
            and outcome.
        g : :obj: `list` of :obj: `function`
            List of link functions for each parameter.
        f : function
            Negative log likelihood function, better be `numpy.ufunc`.
        group_id: :obj: `numpy.ndarray`, optional
            Optional integer group id, gives the way of grouping the random
            effects. When it is not `None`, it should have length `m`.
        offset: `list` of :obj: `np.array`, optional
            Optional list of offsets to apply for each parameter. Must be of length l
            and each element must be None or an np.array of length m
        weights: :obj: `np.ndarray`, optional
            Optional list of weights to apply to the log likelihood likelihood
            Should be of dimension m x n
        normalize_X: bool
            Whether or not to normalize the covariates
        """
        self.model_type = None
        self.parameters = None

        # check to make sure that if we aren't adding intercepts
        # there are covariates on each of the parameter / outcomes
        if any([outcome is None for parameter in X for outcome in parameter]) and not add_intercepts:
            raise RuntimeError("Cannot fit a model without an intercept and no covariates!"
                               "Use add_intercept = True.")

        # dimension
        self.m = m
        self.n = n
        self.l = l
        self.d = d

        # grouping of the random effects
        if group_id is None:
            self.group_id = np.arange(self.m)
        else:
            self.group_id = group_id

        # offset for each parameter
        if offset is None:
            self.offset = [np.ones((self.m, 1))] * self.l
        else:
            self.offset = [off if off is not None else np.ones((self.m, 1)) for off in offset]

        # weights to put on the negative log likelihood
        if weights is None:
            self.W = np.ones((self.m, self.n))
        else:
            self.W = weights

        # data
        self.Y = Y

        # use this later to grab only the covariate indices that were not on splines
        self.cs = [[list(range(k.shape[1])) if k is not None else list() for k in j] for j in X]

        # create the spline specifications
        if spline_specs is not None:
            self.xs = [[[
                spline_design_mat(
                    array=g['spline_var'],
                    knots_type=g['knots_type'],
                    knots_num=g['knots_num'],
                    degree=g['degree'],
                    l_linear=g['l_linear'],
                    r_linear=g['r_linear']
                ) for g in g_dict] if g_dict is not None else None for g_dict in s] for s in spline_specs]
        else:
            self.xs = None

        # create splines
        if spline_specs is not None:
            S = [[
                np.concatenate([
                    self.xs[k][j][i].design_mat(g['spline_var'])[:, 1:] for i, g in enumerate(g_dict)
                ], axis=1) if g_dict is not None else None for j, g_dict in enumerate(s)]
                for k, s in enumerate(spline_specs)]
        else:
            S = None

        # add on an intercept for each parameter
        # and set the index of the first covariate
        # to be either after the intercept or the first covariate
        self.add_intercepts = add_intercepts
        if self.add_intercepts:
            X = self.intercept_X(X=X, m=self.m)
            self.d += 1
        self.ci = int(self.add_intercepts)
        if S is not None:
            self.d = np.array(
                [[dim + spl.shape[1] if spl is not None else dim for dim, spl in zip(o, spline)]
                 for o, spline in zip(self.d, S)]
            )
            # add on the full design matrix for the splines, if applicable
            # do this before normalizing the covariates
            X = [[np.concatenate([x, s], axis=1) if s is not None else x for x, s in zip(x_outcome, s_outcome)]
                 for x_outcome, s_outcome in zip(X, S)]

        # center and scale the covariates, but keep the mean and std for use later on
        # if we're not normalizing the covariates, just make the mean 0 and std 1 to avoid
        # if-else computation later.
        if normalize_X:
            self.X_mean = [[k.mean(axis=0) for k in j] for j in X]
            self.X_std = [[k.std(axis=0) for k in j] for j in X]
        else:
            self.X_mean = [[np.zeros(k.shape[1]) for k in j] for j in X]
            self.X_std = [[np.ones(k.shape[1]) for k in j] for j in X]

        # normalize
        self.X = self.normalize_X(X=X)

        # link and log likelihood functions
        self.g = g
        self.f = f

        # check input
        self.check()

        # group the data, including offset, with group_id
        sort_id = np.argsort(self.group_id)
        self.group_id = self.group_id[sort_id]
        for k in range(self.l):
            self.offset[k] = self.offset[k][sort_id]

        self.Y = self.Y[sort_id]
        self.X = self.sort_X(X=self.X, sort_id=sort_id)
        self.W = self.W[sort_id]

        self.unique_group_id, self.group_sizes = np.unique(self.group_id,
                                                           return_counts=True)
        self.num_groups = self.unique_group_id.size

        # fixed effects
        self.beta = [[np.zeros(self.d[k, j])
                      for j in range(self.n)] for k in range(self.l)]

        # random effects and its covariance matrix
        self.U = np.zeros((self.l, self.num_groups, self.n))
        self.D = np.array([np.identity(self.n) for k in range(self.l)])

        # place holder for parameter
        self.P = np.zeros((self.l, self.m, self.n))

        # optimization interface
        self.opt_interface = optimization.OptimizationInterface(self)

    def check(self):
        """Check the type, value and size of the inputs."""
        # types
        LOG.info("Checking the types of inputs...")
        assert isinstance(self.m, int)
        assert isinstance(self.n, int)
        assert isinstance(self.l, int)
        assert isinstance(self.d, np.ndarray)
        assert isinstance(self.group_id, np.ndarray)
        assert self.d.dtype == int
        assert self.group_id.dtype == int
        assert isinstance(self.offset, list)
        for offset_k in self.offset:
            assert isinstance(offset_k, np.ndarray)
        assert isinstance(self.W, np.ndarray)

        assert isinstance(self.Y, np.ndarray)
        assert self.Y.dtype == np.number
        assert isinstance(self.X, list)
        for X_k in self.X:
            assert isinstance(X_k, list)
            for X_kj in X_k:
                assert isinstance(X_kj, np.ndarray)
                assert X_kj.dtype == np.number

        assert isinstance(self.g, list)
        assert all(callable(g_k) for g_k in self.g)
        assert callable(self.f)
        LOG.info("...passed.")

        # values
        LOG.info("Checking the values of inputs...")
        assert self.m > 0
        assert self.n > 0
        assert self.l > 0
        assert np.all(self.d > 0)
        for k in self.X:
            for j in k:
                assert np.isfinite(j).all()
        for offset_k in self.offset:
            assert np.isfinite(offset_k).all()
        assert (self.W >= 0).all()
        LOG.info("...passed.")

        # sizes
        LOG.info("Checking the sizes of inputs...")
        assert self.Y.shape == (self.m, self.n)
        assert len(self.X) == self.l
        assert all(len(self.X[k]) == self.n for k in range(self.l))
        assert all(self.X[k][j].shape == (self.m, self.d[k, j])
                   for k in range(self.l)
                   for j in range(self.n))

        assert len(self.g) == self.l
        assert self.group_id.shape == (self.m,)
        assert len(self.offset) == self.l
        for offset_k in self.offset:
            assert offset_k.shape == (self.m, 1)
        assert self.W.shape == (self.m, self.n)

        LOG.info("...passed.")

    def sort_X(self, X, sort_id):
        """
        Sorts the list of lists of input arrays by the sort ID.
        Args:
            X: list of list of np.ndarray
            sort_id: np.array

        Returns:
            sorted_X: list of list of np.ndarray sorted by sort_id
        """
        sorted_X = deepcopy(X)
        for k in range(self.l):
            for j in range(self.n):
                sorted_X[k][j] = sorted_X[k][j][sort_id]
        return sorted_X

    def intercept_X(self, X, m):
        """
        Adds on an intercept to the covariates matrices passed in.

        Args:
            X: list of list of np.ndarray or None
            m: number of observations

        Returns:
            new_X: list of list of np.ndarray, with an intercept
        """
        new_X = deepcopy(X)
        intercept = np.ones((m, 1))
        for i in range(self.l):
            for j in range(self.n):
                if new_X[i][j] is None:
                    new_X[i][j] = intercept.copy()
                else:
                    new_X[i][j] = np.concatenate((intercept, X[i][j]), axis=1)
        return new_X

    def normalize_X(self, X):
        """
        Subtracts the mean and divides by the standard deviation
        that are saved in self.X_mean and self.X_std for the covariates.
        Assumes that X has an intercept and that we're not going to normalize that!

        Args:
            X: list of list of np.ndarray

        Returns:
            X_list: list of list of np.ndarray, normalized by self.X_mean and self.X_std
        """
        X_list = deepcopy(X)
        for i in range(self.l):
            for j in range(self.n):
                X_list[i][j][:, self.ci:] = ((X_list[i][j][:, self.ci:] - self.X_mean[i][j][self.ci:]) /
                                             self.X_std[i][j][self.ci:])
        return X_list

    def compute_P(self, X, m, group_sizes, offset, beta=None, U=None):
        """Compute the parameter matrix.

        Parameters
        ----------
        X : :obj: `list` of :obj: `list` of :obj: `numpy.ndarray`
            Covariates matrix
        m : `int`
            Number of individuals
        group_sizes : :obj: `np.ndarray` indicating the sizes of each group
        beta : :obj: `list` of :obj: `list` of :obj: `numpy.ndarray`, optional
            Fixed effects for predicting the parameters.
        U : :obj: `numpy.ndarray`, optional
            Random effects for predicting the parameters. Assume random effects
            follow multi-normal distribution.
        offset: `list` of :obj: `numpy.ndarray`

        Returns
        -------
        array_like
            Parameters for each individual and outcome.
        """
        if beta is None:
            beta = self.beta
        if U is None:
            U = self.U

        P = np.array([X[k][j].dot(beta[k][j])
                      for k in range(self.l)
                      for j in range(self.n)])
        P = P.reshape((self.l, self.n, m)).transpose(0, 2, 1)
        U = np.repeat(U, group_sizes, axis=1)
        P = P + U
        for k in range(self.l):
            P[k] = self.g[k](P[k])
        for k in range(self.l):
            P[k] = P[k] * offset[k]
        return P

    def update_params(self, beta=None, U=None, D=None, P=None):
        """Update the variables related to the parameters.

        Parameters
        ----------
        beta : :obj: `list` of :obj: `list` of :obj: `numpy.ndarray`, optional
            Fixed effects for predicting the parameters.
        U : :obj: `numpy.ndarray`, optional
            Random effects for predicting the parameters. Assume random effects
            follow multi-normal distribution.
        D : :obj: `numpy.ndarray`, optional
            Covariance matrix for the random effects distribution.
        P : :obj: `numpy.ndarray`, optional
            Parameters for each individual and outcome. If `P` is provided,
            the `self.P` will be overwrite by its value, otherwise,
            the `self.P` will be updated by the fixed and random effects.

        """
        if beta is not None:
            self.beta = beta
        if U is not None:
            self.U = U
        if D is not None:
            self.D = D
        if P is not None:
            self.P = P
        else:
            self.P = self.compute_P(
                X=self.X, m=self.m, group_sizes=self.group_sizes, offset=self.offset
            )

    def neg_log_likelihood(self, beta=None, U=None, D=None):
        """Return the negative log likelihood of the model.

        Parameters
        ----------
        beta : :obj: `list` of :obj: `list` of :obj: `numpy.ndarray`, optional
            Fixed effects for predicting the parameters.
        U : :obj: `numpy.ndarray`, optional
            Random effects for predicting the parameters. Assume random effects
            follow multi-normal distribution.
        D : :obj: `numpy.ndarray`, optional
            Covariance matrix for the random effects distribution.

        Returns
        -------
        float
            Average log likelihood.
        """
        if beta is None:
            beta = self.beta
        if U is None:
            U = self.U
        if D is None:
            D = self.D

        P = self.compute_P(beta=beta, U=U, m=self.m, X=self.X,
                           group_sizes=self.group_sizes, offset=self.offset)
        # data negative log likelihood
        val = np.mean(np.sum(self.f(self.Y, P) * self.W, axis=1))
        # random effects prior
        for k in range(self.l):
            val += 0.5*np.mean(np.sum(U[k].dot(np.linalg.pinv(D[k]))*U[k],
                                      axis=1))

        return val

    def optimize_params(self,
                        max_iters=10,
                        optimize_beta=True,
                        optimize_U=True,
                        compute_D=True,
                        rel_tol=None,
                        max_beta_iters=1e3,
                        max_U_iters=1e3):
        """Optimize the parameters.

        Parameters
        ----------
        max_iters : :obj: int, optional
            Maximum number of iterations.
        optimize_beta: :obj: bool, optional
            Indicate if optimize beta every iteration.
        optimize_U: :obj: bool, optional
            Indicate if optimize U every iteration.
        compute_D: :obj: bool, optional
            Indicate if compute D every iteration.
        rel_tol: int, optional
            Relative tolerance to achieve. If rel_tol is achieved
            before the max_iters, then the optimization will terminate.
        max_beta_iters: int, optional
            Maximum number of iterations for scipy.optimize for beta, in every
            max_iters iteration
        max_U_iters: int, option
            Maximum number of iterations for scipy.optimize for U, in every
            max_iters iteration
        """
        LOG.info("Optimizing the parameters.")
        for i in range(max_iters):
            LOG.info(f"On iteration {i}...")
            error = 0
            if optimize_beta:
                old_beta = deepcopy(self.beta)
                self.opt_interface.optimize_beta(maxiter=max_beta_iters)
                beta_error = utils.relative_error(
                    old=utils.beta_to_vec(old_beta),
                    new=utils.beta_to_vec(self.beta)
                )
                error += beta_error
                LOG.debug(f"current beta is {self.beta} \nrelative error {beta_error}")
            if optimize_U:
                old_U = deepcopy(self.U)
                self.opt_interface.optimize_U(maxiter=max_U_iters)
                U_error = utils.relative_error(
                    old=old_U, new=self.U
                )
                error += U_error
                LOG.debug(f"current U is {self.U} \nrelative error {U_error}")
            if compute_D:
                old_D = deepcopy(self.D)
                self.opt_interface.compute_D()
                D_error = utils.relative_error(
                    old=np.array([d[np.triu_indices(self.n)] for d in old_D]),
                    new=np.array([d[np.triu_indices(self.n)] for d in self.D])
                )
                error += D_error
                LOG.debug(f"current D is {self.D} \nrelative error {D_error}")
            total_error = error / (optimize_beta + optimize_U + compute_D)
            LOG.debug(f"total error is {total_error}")
            if rel_tol is not None:
                if total_error <= rel_tol:
                    LOG.info(f"optimization converged with tolerance {rel_tol} after {i} iterations")
                    break
            LOG.info("objective function value %8.2e" % self.neg_log_likelihood())

    def check_new_X(self, X, group_id):
        """
        Check a new X matrix and associated group ID to make sure
        dimensions and types line up with what is expected and was used to fit the model.

        Args:
            X : :obj: `list` of :obj: `list` of :obj: `numpy.ndarray`
                List of list of 2D arrays, storing the covariates for each parameter
                and outcome.
            group_id: :obj: `numpy.ndarray` way of grouping the random effects
        """
        assert isinstance(X, list)
        for X_k in X:
            assert isinstance(X_k, list)
            for X_kj in X_k:
                assert isinstance(X_kj, np.ndarray)
                assert X_kj.dtype == np.number
        assert len(X) == self.l
        assert all(len(X[k]) == self.n for k in range(self.l))
        assert all(X[k][j].shape == (len(group_id), self.d[k, j])
                   for k in range(self.l)
                   for j in range(self.n))

    def compute_new_P(self, X, group_id, offset):
        """
        Makes a parameter matrix for new data. Most of the work in this function
        comes from having to figure out which indices of self.U to use in order to add
        on the random effects, and filling in zeros when there are new random effects
        that were not present in the fitting of the model.

        Args:
            X : :obj: `list` of :obj: `list` of :obj: `numpy.ndarray`
                List of list of 2D arrays, storing the covariates for each parameter
                and outcome.
            group_id: :obj: `numpy.ndarray` way of grouping the random effects
            offset: `list` of :obj: `numpy.ndarray`

        Returns: like
        """
        random_effect_id = group_id.copy()
        offsets = deepcopy(offset)
        # Sort X by the group ID
        sort_id = np.argsort(random_effect_id)
        reverse_sort_id = np.argsort(sort_id)
        random_effect_id = random_effect_id[sort_id]
        for k in range(len(offsets)):
            offsets[k] = offsets[k][sort_id]
        sorted_X = self.sort_X(X=X, sort_id=sort_id)
        # Get the present unique groups, and their sizes
        present_groups, group_sizes = np.unique(random_effect_id, return_counts=True)
        # Figure out what indices of the groups the model was fit on
        # apply to the present groups
        group_indices = np.where(np.in1d(self.unique_group_id, present_groups))[0]
        # See if there are groups in the new data that were not present in the
        # groups that the model was fit on
        existing_random_effects = np.isin(present_groups, self.unique_group_id)
        # Append zeros to the end of U so that when we index in axis 1 from U,
        # getting the index = self.num_groups gets the last row
        zero_random_effects = np.zeros((self.l, 1, self.n))
        U = np.append(self.U, zero_random_effects, axis=1)
        # Get the indices of U that we should slice. If existing random effect,
        # then this will pull the U index from group_indices,
        # else it is the last one that we filled with zeros (num_groups)
        indices_u = np.full(existing_random_effects.shape, self.num_groups)
        indices_u[existing_random_effects] = group_indices
        indices_u = indices_u.astype(int)
        # Get U, and use it to create P
        U = U[:, indices_u, :]
        P = self.compute_P(
            X=sorted_X, m=len(random_effect_id),
            group_sizes=group_sizes, U=U, offset=offsets
        )
        return P[:, reverse_sort_id, :]

    @staticmethod
    def mean_outcome(P):
        raise RuntimeError("This method needs to be over-written with a relevant mean_outcome"
                           "function for a model. Make sure you are not using this class directly. Subclass it"
                           "and over-write this method in your subclass.")

    def predict(self, X, m, spline_specs, group_id=None, offset=None):
        """
        Predict the outcome matrix given a new X matrix and optional group IDs. If the group IDs
        don't fit the group IDs used to fit the model, then no random effects will be added on.
        Args:
            X : :obj: `list` of :obj: `list` of :obj: `numpy.ndarray`
                List of list of 2D arrays, storing the covariates for each parameter
                and outcome (or None instead of array if no covariates)
            m: int
                Number of observations
            spline_specs: :obj: `list` of :obj: `list` of :obj: `list` of `dict`
            group_id: :obj: `numpy.ndarray`, optional
                Optional integer group id, gives the way of grouping the random
                effects. When it is not `None`, it should have length `m`.
            offset: `list` of :obj: `numpy.ndarray`, optional
        """
        if self.add_intercepts:
            LOG.info("Adding an intercept because it was added in the original model."
                     "If this is incorrect, please take away the existing intercept, or fit a new model.")
        if spline_specs is not None:
            S = [[
                np.concatenate([
                    self.xs[k][j][i].design_mat(g['spline_var'])[:, 1:] for i, g in enumerate(g_dict)
                ], axis=1) if g_dict is not None else None for j, g_dict in enumerate(s)]
                for k, s in enumerate(spline_specs)]
        else:
            S = None

        X = self.intercept_X(X=X, m=m)
        if S is not None:
            X = [[np.concatenate([x, s], axis=1) if s is not None else x for x, s in zip(x_outcome, s_outcome)]
                 for x_outcome, s_outcome in zip(X, S)]
        normal_X_with_intercept = self.normalize_X(X=X)
        if group_id is None:
            # Get the number of rows in the very first X matrix
            group_id = np.arange(m)
        # offset for each parameter
        if offset is None:
            offset = [np.ones((m, 1))] * self.l
        else:
            offset = [off if off is not None else np.ones((m, 1)) for off in offset]

        # Check the type and dimensions of X and the groups
        self.check_new_X(X=normal_X_with_intercept, group_id=group_id)

        # Compute a new parameter matrix based on X and the group ids,
        # and the existing U and beta from self
        P = self.compute_new_P(X=normal_X_with_intercept, group_id=group_id, offset=offset)

        # Get the new predictions as fitted values for a new parameter matrix P
        predictions = self.mean_outcome(P=P)
        return predictions

    def summarize(self, file=None):
        """
        Output summaries of the model results.

        Returns: (str)
        """
        message = list()
        message.append(f"MODEL SUMMARY FOR {self.model_type.upper()}")
        message.append("------------------------------------------")
        message.append("------------------------------------------")
        message.append(f"NUM OBSERVATIONS: {self.m}")
        message.append(f"NUM PARAMETERS: {self.n}")
        message.append(f"NUM OUTCOMES: {self.l}")
        message.append("------------------------------------------")
        message.append("FIXED EFFECTS")
        message.append("------------------------------------------")
        message.append("UNTRANSFORMED")
        for i in range(self.l):
            message.append(f"\n{self.parameters[i].upper()}")
            for j in range(self.n):
                message.append(f"OUTCOME {j}")
                if self.add_intercepts:
                    message.append(f"value for observations with average covariate values: {self.beta[i][j][0]}")
                message.append(f"estimated coefficients: "
                               f"{self.beta[i][j][self.ci:][self.cs[i][j]] / self.X_std[i][j][self.ci:][self.cs[i][j]]}")
        message.append("\nTRANSFORMED")
        for i in range(self.l):
            message.append(f"\n{self.parameters[i].upper()}")
            for j in range(self.n):
                message.append(f"OUTCOME {j}")
                if self.add_intercepts:
                    message.append(f"value for observations with average covariate values: {self.g[i](self.beta[i][j][0])}")
                message.append(f"estimated coefficients: {self.g[i](self.beta[i][j][self.ci:][self.cs[i][j]] / self.X_std[i][j][self.ci:][self.cs[i][j]])}")
        message.append("------------------------------------------")
        message.append("RANDOM EFFECTS")
        message.append("------------------------------------------")
        message.append("RANDOM EFFECTS VARIANCE-COVARIANCE MATRIX")
        for i in range(self.l):
            message.append(f"\n{self.parameters[i].upper()}")
            message.append(f"outcome {i}: \n {self.D[i]}")
        message.append("------------------------------------------")
        message.append("RANDOM EFFECTS BY GROUP")
        for i in range(self.l):
            message.append(f"\n{self.parameters[i].upper()}")
            for j in range(self.num_groups):
                message.append(f"group id {j}: {self.U[i][j]}")

        message = '\n'.join(message)
        if file is not None:
            print(f"Printing to {file}")
            with open(file, 'w') as f:
                print(message, file=f)
        else:
            print(message)
