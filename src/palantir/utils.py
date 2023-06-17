from typing import Iterable
from warnings import warn
import pandas as pd
import numpy as np

import phenograph

from scipy.sparse import csr_matrix, find, issparse
from scipy.sparse.linalg import eigs
import scanpy as sc


class CellNotFoundException(Exception):
    """Exception raised when no valid component is found for the provided cell type."""

    pass


def run_pca(data, n_components=300, use_hvg=True):
    """Run PCA

    :param data: Dataframe of cells X genes. Typicaly multiscale space diffusion components
    :param n_components: Number of principal components
    :return: PCA projections of the data and the explained variance
    """
    if type(data) is sc.AnnData:
        ad = data
    else:
        ad = sc.AnnData(data.values)

    # Run PCA
    if not use_hvg:
        n_comps = n_components
    else:
        sc.pp.pca(ad, n_comps=1000, use_highly_variable=True, zero_center=False)
        try:
            n_comps = np.where(np.cumsum(ad.uns["pca"]["variance_ratio"]) > 0.85)[0][0]
        except IndexError:
            n_comps = n_components

    # Rerun with selection number of components
    sc.pp.pca(ad, n_comps=n_comps, use_highly_variable=use_hvg, zero_center=False)

    # Return PCA projections if it is a dataframe
    pca_projections = pd.DataFrame(ad.obsm["X_pca"], index=ad.obs_names)
    return pca_projections, ad.uns["pca"]["variance_ratio"]


def run_diffusion_maps(data_df, n_components=10, knn=30, alpha=0, seed=None):
    """Run Diffusion maps using the adaptive anisotropic kernel

    :param data_df: PCA projections of the data or adjacency matrix
    :param n_components: Number of diffusion components
    :param knn: Number of nearest neighbors for graph construction
    :param alpha: Normalization parameter for the diffusion operator
    :param seed: Numpy random seed, randomized if None, set to an arbitrary integer for reproducibility
    :return: Diffusion components, corresponding eigen values and the diffusion operator
    """

    # Determine the kernel
    N = data_df.shape[0]
    if not issparse(data_df):
        print("Determing nearest neighbor graph...")
        temp = sc.AnnData(data_df.values)
        sc.pp.neighbors(temp, n_pcs=0, n_neighbors=knn)
        kNN = temp.obsp["distances"]

        # Adaptive k
        adaptive_k = int(np.floor(knn / 3))
        adaptive_std = np.zeros(N)

        for i in np.arange(len(adaptive_std)):
            adaptive_std[i] = np.sort(kNN.data[kNN.indptr[i] : kNN.indptr[i + 1]])[
                adaptive_k - 1
            ]

        # Kernel
        x, y, dists = find(kNN)

        # X, y specific stds
        dists = dists / adaptive_std[x]
        W = csr_matrix((np.exp(-dists), (x, y)), shape=[N, N])

        # Diffusion components
        kernel = W + W.T
    else:
        kernel = data_df

    # Markov
    D = np.ravel(kernel.sum(axis=1))

    if alpha > 0:
        # L_alpha
        D[D != 0] = D[D != 0] ** (-alpha)
        mat = csr_matrix((D, (range(N), range(N))), shape=[N, N])
        kernel = mat.dot(kernel).dot(mat)
        D = np.ravel(kernel.sum(axis=1))

    D[D != 0] = 1 / D[D != 0]
    T = csr_matrix((D, (range(N), range(N))), shape=[N, N]).dot(kernel)
    # Eigen value dcomposition
    np.random.seed(seed)
    v0 = np.random.rand(min(T.shape))
    D, V = eigs(T, n_components, tol=1e-4, maxiter=1000, v0=v0)
    D = np.real(D)
    V = np.real(V)
    inds = np.argsort(D)[::-1]
    D = D[inds]
    V = V[:, inds]

    # Normalize
    for i in range(V.shape[1]):
        V[:, i] = V[:, i] / np.linalg.norm(V[:, i])

    # Create are results dictionary
    res = {"T": T, "EigenVectors": V, "EigenValues": D}
    res["EigenVectors"] = pd.DataFrame(res["EigenVectors"])
    if not issparse(data_df):
        res["EigenVectors"].index = data_df.index
    res["EigenValues"] = pd.Series(res["EigenValues"])
    res["kernel"] = kernel

    return res


def run_magic_imputation(data, dm_res, n_steps=3):
    """Run MAGIC imputation

    :param dm_res: Diffusion map results from run_diffusion_maps
    :param n_steps: Number of steps in the diffusion operator
    :return: Imputed data matrix
    """
    if type(data) is sc.AnnData:
        data = pd.DataFrame(
            data.X.todense(), index=data.obs_names, columns=data.var_names
        )

    T_steps = dm_res["T"] ** n_steps
    imputed_data = pd.DataFrame(
        np.dot(T_steps.todense(), data), index=data.index, columns=data.columns
    )

    return imputed_data


def determine_multiscale_space(dm_res, n_eigs=None):
    """Determine multi scale space of the data

    :param dm_res: Diffusion map results from run_diffusion_maps
    :param n_eigs: Number of eigen vectors to use. If None specified, the number
            of eigen vectors will be determined using eigen gap
    :return: Multi scale data matrix
    """
    if n_eigs is None:
        vals = np.ravel(dm_res["EigenValues"])
        n_eigs = np.argsort(vals[: (len(vals) - 1)] - vals[1:])[-1] + 1
        if n_eigs < 3:
            n_eigs = np.argsort(vals[: (len(vals) - 1)] - vals[1:])[-2] + 1

    # Scale the data
    use_eigs = list(range(1, n_eigs))
    eig_vals = np.ravel(dm_res["EigenValues"][use_eigs])
    data = dm_res["EigenVectors"].values[:, use_eigs] * (eig_vals / (1 - eig_vals))
    data = pd.DataFrame(data, index=dm_res["EigenVectors"].index)

    return data


def run_tsne(data, n_dim=2, perplexity=150, **kwargs):
    """Run tSNE

    :param data: Dataframe of cells X genes. Typicaly multiscale space diffusion components
    :param n_dim: Number of dimensions for tSNE embedding
    :return: tSNE embedding of the data
    """
    try:
        from MulticoreTSNE import MulticoreTSNE as TSNE

        print("Using the 'MulticoreTSNE' package by Ulyanov (2017)")
        tsne = TSNE(n_components=n_dim, perplexity=perplexity, **kwargs).fit_transform(
            data.values
        )
    except ImportError:
        from sklearn.manifold import TSNE

        print(
            "Could not import 'MulticoreTSNE'. Install for faster runtime. Falling back to scikit-learn."
        )
        tsne = TSNE(n_components=n_dim, perplexity=perplexity, **kwargs).fit_transform(
            data.values
        )

    tsne = pd.DataFrame(tsne, index=data.index)
    tsne.columns = ["x", "y"]
    return tsne


def determine_cell_clusters(data, k=50):
    """Run phenograph for clustering cells

    :param data: Principal components of the data.
    :param k: Number of neighbors for kNN graph construction
    :return: Clusters
    """
    # Cluster and cluster centrolds
    communities, _, _ = phenograph.cluster(data.values, k=k)
    communities = pd.Series(communities, index=data.index)
    return communities


def _return_cell(ec, obs_names, celltype, mm, dcomp):
    """
    Helper function to print and return the early cell.

    Args:
        ec (int): Index of the early cell.
        obs_names (list): Names of cells.
        celltype (str): The cell type of interest.
        mm (str): Max/min status of the diffusion component.
        dcomp (int): Index of diffusion component.

    Returns:
        str: Name of the early cell.
    """
    early_cell = obs_names[ec]
    print(
        f"Using {early_cell} for cell type {celltype} which is {mm} in "
        f"diffusion component {dcomp}."
    )
    return early_cell


def early_cell(ad, celltype, celltype_column="anno", fallback_seed=None):
    """
    Helper to determine 'early_cell' for 'palantir.core.run_palantir'.
    Finds cell of 'celltype' at the extremes of the state space represented by diffusion maps.

    Args:
        ad (AnnData): Annotated data matrix.
        celltype (str): The cell type of interest.
        celltype_column (str): Column name in the data matrix where the cell
        type information is stored. Default is 'anno'.
        fallback_seed (int): Seed for random number generator in fallback method.
        Default is None.

    Returns:
        str: Name of the terminal cell for the given cell type.

    Raises:
        CellNotFoundException: If no valid component is found for the provided cell type.
    """
    for dcomp in range(ad.obsm["DM_EigenVectors"].shape[1]):
        ec = ad.obsm["DM_EigenVectors"][:, dcomp].argmax()
        if ad.obs[celltype_column][ec] == celltype:
            return _return_cell(ec, ad.obs_names, celltype, "max", dcomp)
        ec = ad.obsm["DM_EigenVectors"][:, dcomp].argmin()
        if ad.obs[celltype_column][ec] == celltype:
            return _return_cell(ec, ad.obs_names, celltype, "min", dcomp)

    if fallback_seed is not None:
        print("Falling back to slow early cell detection.")
        return fallback_terminal_cell(
            ad, celltype, celltype_column=celltype_column, seed=fallback_seed
        )

    raise CellNotFoundException(
        f"No valid component found: {celltype} "
        "Consider increasing the number of diffusion components "
        "('n_components' in palantir.utils.run_diffusion_maps) "
        "or specify a 'fallback_seed' to determine an early cell based on "
        f"reverse pseudotime starting from random non-{celltype} cell."
    )


def fallback_terminal_cell(ad, celltype, celltype_column="anno", seed=2353):
    """
    Fallback method to find terminal cells when no valid diffusion component
    is found for the provided cell type.

    Args:
        ad (AnnData): Annotated data matrix.
        celltype (str): The cell type of interest.
        celltype_column (str): Column name in the data matrix where the cell
        type information is stored. Default is 'anno'.
        seed (int): Seed for random number generator. Default is 2353.

    Returns:
        str: Name of the terminal cell for the given cell type.
    """
    other_cells = ad.obs_names[ad.obs[celltype_column] != celltype]
    fake_early_cell = other_cells.to_series().sample(1, random_state=seed)[0]
    pr_res = palantir.core.run_palantir(
        ms_data,
        fake_early_cell,
        terminal_states=None,
        use_early_cell_as_start=True,
    )
    idx = ad.obs[celltype_column] == celltype
    ec = pr_res.pseudotime[idx].argmax()
    early_cell = ad.obs_names[ec]
    print(
        f"Using {early_cell} for cell type {celltype} which is latest cell in "
        "{celltype} when starting from {fake_early_cell}."
    )
    return early_cell


def find_terminal_states(
    ad: sc.AnnData,
    celltypes: Iterable,
    celltype_column: str = "anno",
    fallback_seed: int = None,
):
    """
    Identifies terminal states for a list of cell types in the AnnData object.

    This function iterates over the provided cell types, attempting to find a terminal cell for each one
    using the `palantir.utils.early_cell` function. In cases where no valid component is found for a cell type,
    it emits a warning and skips to the next cell type.

    Parameters
    ----------
    ad : AnnData
        Annotated data matrix from Scanpy. It should contain computed diffusion maps.
    celltypes : Iterable
        An iterable (like a list or tuple) of cell type names for which terminal states are to be identified.
    celltype_column : str, optional
        Column name in the AnnData object where the cell type information is stored. Default is 'anno'.
    fallback_seed : int, optional
        Seed for the random number generator used in the fallback method of `palantir.utils.early_cell` function.
        Defaults to None, in which case the RNG will be seeded randomly.

    Returns
    -------
    pd.Series
        A pandas Series where the index are the cell types and the values are the names of the terminal cells.
        If no terminal cell is found for a cell type, it will not be included in the series.
    """
    terminal_states = pd.Series()
    for ct in celltypes:
        try:
            cell = palantir.utils.early_cell(ad, ct, celltype_column, fallback_seed)
        except CellNotFoundException:
            warn(
                f"No valid component found: {celltype} "
                "Consider increasing the number of diffusion components "
                "('n_components' in palantir.utils.run_diffusion_maps). "
                "The cell type {celltype} will be sckipped."
            )
            continue
        terminal_states[ct] = cell
    return terminal_states
