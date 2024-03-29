# This file is a tutorial for waveguicsx (*), whose inputs are
# the matrices K0, K1, K2, M and the excitation vector F.
# In the tutorial, K0, K1, K2 and M are finite element matrices generated by FEnicSX (**).
#  (*) waveguicsx is a python library for solving complex waveguide problems
#      Copyright (C) 2023-2024  Fabien Treyssede
#      waveguicsx is free software distributed under the GNU General Public License
#      (https://github.com/treyssede/waveguicsx)
# (**) FEniCSx is an open-source computing platform for solving partial differential equations
#      distributed under the GNU Lesser General Public License (https://fenicsproject.org/)

##################################
# 3D (visco-)elastic waveguide example\
# The cross-section is a 2D square with free boundary conditions on its 1D boundaries\
# Material: viscoelastic steel\
# The waveguide FE formulation (SAFE) leads to the following eigenvalue problem:\
# $(\textbf{K}_1-\omega^2\textbf{M}+\text{i}k(\textbf{K}_2+\textbf{K}_2^\text{T})+k^2\textbf{K}_3)\textbf{U}=\textbf{0}$\
# Viscoelastic loss is included by introducing imaginary parts (negative) to wave celerities\
# In this example:
# - the parameter loop (here, the frequency loop) is distributed on all processes
# - FE mesh and matrices are built on each local process
# Reminder for an execution in parallel mode (e.g. 4 processes):
#  mpiexec -n 4 python3 Elastic_Waveguide_SquareBar3D_ParallelizedLoop.py

import dolfinx
import ufl
from mpi4py import MPI
from petsc4py import PETSc
from slepc4py import SLEPc
import numpy as np
import matplotlib.pyplot as plt
#import pyvista

from waveguicsx.waveguide import Waveguide
#For proper use with a jupyter notebook, uncomment the following line:
#pyvista.set_jupyter_backend("static"); pyvista.start_xvfb() #try: "none", "static", "pythreejs", "ipyvtklink"...

##################################
# Input parameters
a = 2.7e-3 #square half-length (m)
N = 10 #number of finite elements along one half-side
rho, cs, cl = 7932, 3260, 5960 #core density (kg/m3), shear and longitudinal wave celerities (m/s)
kappas, kappal = 0.008, 0.003 #core shear and longitudinal bulk wave attenuations (Np/wavelength)
omega = 2*np.pi*np.linspace(500e3/1000, 500e3, num=50) #angular frequency range (rad/s)
nev = 20 #number of eigenvalues

##################################
# Re-scaling
L0 = a #characteristic length
T0 = a/cs #characteristic time
M0 = rho*a**3#characteristic mass
a = a/L0
rho, cs, cl = rho/M0*L0**3, cs/L0*T0, cl/L0*T0
omega = omega*T0
cs, cl = cs/(1+1j*kappas/2/np.pi), cl/(1+1j*kappal/2/np.pi) #complex celerities (core)

##################################
# Create mesh and finite elements (six-node triangles with three dofs per node for the three components of displacement)
mesh = dolfinx.mesh.create_rectangle(MPI.COMM_SELF, [np.array([-a, -a]), np.array([a, a])], 
                               [2*N, 2*N], dolfinx.mesh.CellType.triangle) #MPI.COMM_SELF = FE mesh is built on each local process
element = ufl.VectorElement("CG", "triangle", 2, 3) #Lagrange element, triangle, quadratic "P2", 3D vector
V = dolfinx.fem.FunctionSpace(mesh, element)

##################################
# Create Material properties (isotropic)
def isotropic_law(rho, cs, cl):
    E, nu = rho*cs**2*(3*cl**2-4*cs**2)/(cl**2-cs**2), 0.5*(cl**2-2*cs**2)/(cl**2-cs**2)
    C11 = C22 = C33 = E/(1+nu)/(1-2*nu)*(1-nu)
    C12 = C13 = C23 = E/(1+nu)/(1-2*nu)*nu
    C44 = C55 = C66 = E/(1+nu)/2
    return ((C11,C12,C13,0,0,0), 
            (C12,C22,C23,0,0,0), 
            (C13,C23,C33,0,0,0), 
            (0,0,0,C44,0,0), 
            (0,0,0,0,C55,0), 
            (0,0,0,0,0,C66))
C = isotropic_law(rho, cs, cl)
C = dolfinx.fem.Constant(mesh, PETSc.ScalarType(C))

##################################
# Create free boundary conditions
bcs = []

##################################
# Define variational problem (SAFE method)
u = ufl.TrialFunction(V)
v = ufl.TestFunction(V)
Lxy = lambda u: ufl.as_vector([u[0].dx(0), u[1].dx(1), 0, u[0].dx(1)+u[1].dx(0), u[2].dx(0), u[2].dx(1)])
Lz  = lambda u: ufl.as_vector([0, 0, u[2], 0, u[0], u[1]])
k0 = ufl.inner(C*Lxy(u), Lxy(v)) * ufl.dx
k0_form = dolfinx.fem.form(k0)
k1 = ufl.inner(C*Lz(u), Lxy(v)) * ufl.dx
k1_form = dolfinx.fem.form(k1)
k2 = ufl.inner(C*Lz(u), Lz(v)) * ufl.dx
k2_form = dolfinx.fem.form(k2)
m = rho*ufl.inner(u, v) * ufl.dx
mass_form = dolfinx.fem.form(m)

##################################
# Build PETSc matrices
M = dolfinx.fem.petsc.assemble_matrix(mass_form, bcs=bcs, diagonal=0.0)
M.assemble()
K0 = dolfinx.fem.petsc.assemble_matrix(k0_form, bcs=bcs)
K0.assemble()
K1 = dolfinx.fem.petsc.assemble_matrix(k1_form, bcs=bcs, diagonal=0.0)
K1.assemble()
K2 = dolfinx.fem.petsc.assemble_matrix(k2_form, bcs=bcs, diagonal=0.0)
K2.assemble()

##################################
# Solve the eigenproblem with SLEPc\
# The parameter is k, the eigenvalue is omega**2
# The parameter loop is parallelized
# Parallelization
comm = MPI.COMM_WORLD #use all processes for the loop
size = comm.Get_size()  #number of processors
rank = comm.Get_rank()  #returns the rank of the process that called it within comm_world
# Split the parameter range and scatter to all
if rank == 0: #define on rank 0 only
    param_split = np.array_split(omega, size) #split param in blocks of length size roughly
else:
    param_split = None
param_local = comm.scatter(param_split, root=0) #scatter 1 block per process
# Solve
wg = Waveguide(MPI.COMM_SELF, M, K0, K1, K2) #MPI.COMM_SELF = SLEPc will used FE matrices on each local process
wg.set_parameters(omega=param_local)
wg.solve(nev)
# Some post-processing
wg.compute_energy_velocity()
wg.compute_traveling_direction()
# Gather
wg.omega = comm.reduce([wg.omega], op=MPI.SUM, root=0) #reduce works for lists: brackets are necessary (wg.omega is not a list but a numpy array)
wg.eigenvalues = comm.reduce(wg.eigenvalues, op=MPI.SUM, root=0)
wg.energy_velocity = comm.reduce(wg.energy_velocity, op=MPI.SUM, root=0)
wg.traveling_direction = comm.reduce(wg.traveling_direction, op=MPI.SUM, root=0)
#wg.eigenvectors = comm.reduce(wg.eigenvectors, op=MPI.SUM, root=0) #don't do this line: reduce cannot pickle 'petsc4py.PETSc.Vec' objects (keep the mode shapes distributed on each processor rather than gather them)
# Plot results
if rank == 0:
    wg.omega = np.concatenate(wg.omega) #wg.omega is transformed to a numpy array for a proper use of wg.plot()
    wg.plot()
    wg.plot_energy_velocity(direction=+1)
    #plt.savefig("Elastic_Waveguide_Bar3D_ParallelizedLoop.svg")
    plt.show()

