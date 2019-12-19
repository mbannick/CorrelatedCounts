# -*- coding: utf-8 -*-
"""
    Zero-Inflated Poisson Demo
    ~~~~~~~~~~~~~~~~~~~~~~~~~~
"""
from ccount.simulate import HurdlePoissonSimulation
from ccount.models import HurdlePoissonModel
import numpy as np

hp_p_error = []
hp_beta_error = []

i = 0
# while i < 100:
# set up the simulation
m = 100
n = 2
d = [1] * n

p = 0.5
x = [np.ones((m, d[j])) for j in range(n)]
beta = [np.array([0.1] * d[j]) for j in range(n)]
D = np.array([[1.0, 0.1], [0.1, 1.0]])

s_zip = HurdlePoissonSimulation(m=m, n=n, d=d)
s_zip.update_params(p=p, x=x, beta=beta, D=D)

# fit the model
d = np.array([[1] * n, d])
X = [[np.ones((m, 1)) for j in range(n)], x]
# true parameters in the right form
beta = [[np.array([np.log(p / (1.0 - p))]), np.array([np.log(p / (1.0 - p))])],
        [np.array([0.1]), np.array([0.1])]]
Y = s_zip.simulate()

D = np.array([np.eye(n)*1e-10, D])
U = np.array([np.zeros(s_zip.u.shape), s_zip.u])
theta = np.vstack(s_zip.theta).T
P = np.array([np.ones(theta.shape)*p, theta])

m_zip = HurdlePoissonModel(m=m, d=d, Y=Y, X=X)
m_zip.update_params(D=D, U=U)
m_zip.optimize_params(optimize_beta=True,
                      optimize_U=False,
                      compute_D=False,
                      max_iters=1)

error = np.array(m_zip.beta) - np.array(beta)
hp_p_error.append(error[0])
hp_beta_error.append(error[1])

print(f"Estimated beta... {m_zip.beta}")
print(f"True beta........ {beta}")

i += 1

mean_hp_p_error = sum(hp_p_error) / len(hp_p_error)
mean_hp_beta_error = sum(hp_beta_error) / len(hp_beta_error)

print(f"Overall p error for HURDLE is {mean_hp_p_error}")
print(f"Overall beta error for HURDLE is {mean_hp_beta_error}")
