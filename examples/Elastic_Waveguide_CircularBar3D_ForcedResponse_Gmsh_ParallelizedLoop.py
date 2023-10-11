##################################
# 3D elastic waveguide example\
# The cross-section is a 2D circle with free boundary conditions on its 1D boundaries, material: elastic steel\
# The waveguide FE formulation (SAFE) leads to the following eigenvalue problem:\
# $(\textbf{K}_1-\omega^2\textbf{M}+\text{i}k(\textbf{K}_2+\textbf{K}_2^\text{T})+k^2\textbf{K}_3)\textbf{U}=\textbf{0}$\
# This eigenproblem is solved with the varying parameter as the frequency (eigenvalues are then wavenumbers).\
# The forced response is computed for a point force at the center node ot the cross-section.\
# Results are to be compared with Figs. 5, 6 and 7 of paper: Treyssede, Wave Motion 87 (2019), 75-91.
# In this example:
# - the parameter loop (here, the frequency loop) is distributed on all processes
# - FE mesh and matrices are (therefore) built on each local process
# Reminder for an execution in parallel mode (e.g. 8 processes):
#  mpiexec -n 8 python3 Elastic_Waveguide_CircularBar3D_ForcedResponse_Gmsh_ParallelizedLoop.py

import gmsh #full documentation entirely defined in the `gmsh.py' module
import dolfinx
import ufl
from mpi4py import MPI
from petsc4py import PETSc
from slepc4py import SLEPc
import numpy as np
import matplotlib.pyplot as plt
#import pyvista

#pyvista.set_jupyter_backend("none"); pyvista.start_xvfb() #uncomment with jupyter notebook (try also: "static", "pythreejs", "ipyvtklink")

##################################
# Input parameters
a = 2.7e-3 #cross-section radius (m)
le = a/8 #finite element characteristic length (m)
rho, cs, cl = 7800, 3296, 5963 #density (kg/m3), shear and longitudinal wave celerities (m/s)
kappas, kappal = 0*0.008, 0*0.003 #shear and longitudinal bulk wave attenuations (Np/wavelength)
omega = np.arange(0.1, 10.1, 0.1)*cs/a #angular frequencies (rad/s)
nev = 300 #number of eigenvalues

##################################
# Re-scaling
L0 = a #characteristic length
T0 = a/cs #characteristic time
M0 = rho*a**3#characteristic mass
a, le = a/L0, le/L0
rho, cs, cl = rho/M0*L0**3, cs/L0*T0, cl/L0*T0
omega = omega*T0
cs, cl = cs/(1+1j*kappas/2/np.pi), cl/(1+1j*kappal/2/np.pi) #complex celerities

##################################
# Create mesh from Gmsh and finite elements (six-node triangles with three dofs per node for the three components of displacement)
gmsh.initialize()
# Core
origin = gmsh.model.geo.addPoint(+0, 0, 0, le, 1)
gmsh.model.geo.addPoint(+a, 0, 0, le, 2)
gmsh.model.geo.addPoint(0, +a, 0, le, 3)
gmsh.model.geo.addPoint(-a, 0, 0, le, 4)
gmsh.model.geo.addPoint(0, -a, 0, le, 5)
gmsh.model.geo.addCircleArc(2, 1, 3, 1)
gmsh.model.geo.addCircleArc(3, 1, 4, 2)
gmsh.model.geo.addCircleArc(4, 1, 5, 3)
gmsh.model.geo.addCircleArc(5, 1, 2, 4)
gmsh.model.geo.addCurveLoop([1, 2, 3, 4], 1)
disk = gmsh.model.geo.addPlaneSurface([1])
# Physical groups
gmsh.model.geo.synchronize() #the CAD entities must be synchronized with the Gmsh model
gmsh.model.addPhysicalGroup(2, [disk], tag=1) #2: for 2D (surface)
#gmsh.model.addPhysicalGroup(1, [1, 2, 3, 4], tag=11) #1: for 1D (line)
# Generate mesh
gmsh.model.mesh.embed(0, [origin], 2, disk) #ensure node points at the origin
gmsh.model.mesh.generate(2) #generate a 2D mesh
gmsh.model.mesh.setOrder(2) #interpolation order for the geometry, here 2nd order
# From gmsh to fenicsx
mesh, cell_tags, facet_tags = dolfinx.io.gmshio.model_to_mesh(gmsh.model, MPI.COMM_SELF, 0, gdim=2) #MPI.COMM_SELF = FE mesh is built on each local process
# # Reminder for save & read
# gmsh.write("Elastic_Waveguide_Bar3D_Open.msh") #save to disk
# mesh, cell_tags, facet_tags = dolfinx.io.gmshio.read_from_msh("Elastic_Waveguide_Bar3D_Open.msh", MPI.COMM_WORLD, rank=0, gdim=2)
gmsh.finalize() #called when done using the Gmsh Python API
# Finite element space
element = ufl.VectorElement("CG", "triangle", 2, 3) #Lagrange element, triangle, quadratic "P2", 3D vector
V = dolfinx.fem.FunctionSpace(mesh, element)

##################################
# Create Material properties (isotropic)
def isotropic_law(rho, cs, cl):
    E, nu = rho*cs**2*(3*cl**2-4*cs**2)/(cl**2-cs**2), 0.5*(cl**2-2*cs**2)/(cl**2-cs**2)
    C11 = C22 = C33 = E/(1+nu)/(1-2*nu)*(1-nu)
    C12 = C13 = C23 = E/(1+nu)/(1-2*nu)*nu
    C44 = C55 = C66 = E/(1+nu)/2
    return np.array([[C11,C12,C13,0,0,0], 
                     [C12,C22,C23,0,0,0], 
                     [C13,C23,C33,0,0,0], 
                     [0,0,0,C44,0,0], 
                     [0,0,0,0,C55,0], 
                     [0,0,0,0,0,C66]])
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
k1 = ufl.inner(C*Lxy(u), Lxy(v)) * ufl.dx
k1_form = dolfinx.fem.form(k1)
k2 = ufl.inner(C*Lz(u), Lxy(v)) * ufl.dx
k2_form = dolfinx.fem.form(k2)
k3 = ufl.inner(C*Lz(u), Lz(v)) * ufl.dx
k3_form = dolfinx.fem.form(k3)
m = rho*ufl.inner(u, v) * ufl.dx
mass_form = dolfinx.fem.form(m)

##################################
# Build PETSc matrices
M = dolfinx.fem.petsc.assemble_matrix(mass_form, bcs=bcs, diagonal=0.0)
M.assemble()
K0 = dolfinx.fem.petsc.assemble_matrix(k1_form, bcs=bcs)
K0.assemble()
K1 = dolfinx.fem.petsc.assemble_matrix(k2_form, bcs=bcs, diagonal=0.0)
K1.assemble()
K2 = dolfinx.fem.petsc.assemble_matrix(k3_form, bcs=bcs, diagonal=0.0)
K2.assemble()

##################################
# Excitation force definition (point force)
dof_coords = V.tabulate_dof_coordinates()
x0 = np.array([0, 0, 0]) #desired coordinate of point force
dof = int(np.argmin(np.linalg.norm(dof_coords - x0, axis=1))) #find nearest dof
print(f'Point force coordinates (nearest dof):  {(dof_coords[dof,:])}') #check
F0 = M.createVecRight()
dof = dof*3 + 2 #orientation along z
dof = dof - 2 #uncomment this line for an orientation along x instead
F0[dof] = 1
##Uncomment lines below for distributed excitation
#body_force = dolfinx.fem.Constant(mesh, PETSc.ScalarType((0, 0, 1))) #body force of unit amplitude along z
#traction = dolfinx.fem.Constant(mesh, PETSc.ScalarType((0, 0, 0))) #zero traction
#ds = ufl.Measure("ds", domain=mesh)
#f = ufl.inner(body_force, v) * ufl.dx + ufl.inner(traction, v) * ds
#f_form = dolfinx.fem.form(f)
#F = dolfinx.fem.petsc.assemble_vector(f_form)
#F.assemble()

##################################
# Parallelization
comm = MPI.COMM_WORLD #use all processes for the loop
size = comm.Get_size()  #number of processors
rank = comm.Get_rank()  #returns the rank of the process that called it within comm_world
# Split the parameter range and scatter to all
if rank == 0: #define on rank 0 only
    omega_split = np.array_split(omega, size) #split param in blocks of length size roughly
else:
    omega_split = None
omega_local = comm.scatter(omega_split, root=0) #scatter 1 block per process

##################################
# Solve the eigenproblem with SLEPc (the parameter is omega, the eigenvalue is k)
from waveguicsx.waveguide import Waveguide
wg = Waveguide(MPI.COMM_SELF, M, K0, K1, K2) #MPI.COMM_SELF = SLEPc will used FE matrices on each local process
wg.set_parameters(omega=omega_local)
wg.evp.setTolerances(tol=1e-10, max_it=20)
wg.solve(nev=nev, target=0) #access to components with: wg.eigenvalues[ik][imode], wg.eigenvectors[ik][idof,imode]

##################################
# Computation of excitability and forced response
wg.compute_response_coefficient(F=F0, dof=dof)
frequency, response = wg.compute_response(dof=dof, z=5, spectrum=None) #spectrum=excitation.spectrum

##################################
# Gather
wg.omega = comm.reduce([wg.omega], op=MPI.SUM, root=0) #reduce works for lists: brackets are necessary (wg.omega is not a list but a numpy array)
wg.eigenvalues = comm.reduce(wg.eigenvalues, op=MPI.SUM, root=0)
#wg.eigenvectors = comm.reduce(wg.eigenvectors, op=MPI.SUM, root=0) #don't do this line: reduce cannot pickle 'petsc4py.PETSc.Vec' objects (keep the mode shapes distributed on each processor rather than gather them)
wg.coefficient = comm.reduce(wg.coefficient, op=MPI.SUM, root=0)
wg.excitability = comm.reduce(wg.excitability, op=MPI.SUM, root=0)
frequency = comm.reduce([frequency], op=MPI.SUM, root=0)
response = comm.reduce([response], op=MPI.SUM, root=0)

##################################
# Plots
if rank == 0:
    wg.omega = np.concatenate(wg.omega) #wg.omega is transformed to a numpy array for a proper use of wg.plot()
    wg.plot()
    wg.plot_coefficient()
    ax = wg.plot_excitability()
    ax.set_yscale('log')
    ax.set_ylim(1e-3,0.5e+1)
    frequency = np.concatenate(frequency)
    response = np.concatenate(response) #use np.concatenate(response, axis=1)  if z contains more than one value
    fig, ax = plt.subplots(1, 1)  
    ax.plot(frequency, np.abs(response.T), linewidth=1, linestyle="-", color="k")
    ax.set_xlabel('frequency')
    ax.set_ylabel('|u|')
    ax.set_yscale('log')
    ax.set_ylim(1e-2,1e+1)
    fig.tight_layout()
    #plt.savefig("figure_name.svg")
    plt.show()
