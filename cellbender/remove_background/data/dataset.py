"""Class and functions for working with a count matrix dataset."""

import tables
import numpy as np
import scipy.sparse as sp
import cellbender.remove_background.model
import cellbender.remove_background.data.transform as trans
from sklearn.decomposition import PCA
import torch
from scipy.stats import mode

from typing import Dict, List, Union, Tuple
import logging
import os

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # This needs to be after matplotlib.use('Agg')


class Dataset:
    """Object for storing scRNA-seq count matrix data and basic manipulations.

    Args:
        transformation: Transformation to be applied to count data.
        input_file: Input data file path.
        expected_cell_count: Expected number of real cells a priori.
        num_transition_barcodes: Number of droplets after the cells that could
            possibly have cells.
        model_name: Model to use.
        gene_blacklist: List of integer indices of genes to exclude entirely.
        low_count_threshold: Droplets with UMI counts below this number are
            excluded entirely from the analysis.

    Attributes:
        input_file: Name of data source file.
        analyzed_barcode_inds: numpy.ndarray of indices that denote which
            barcodes, as indexed in the original dataset, are being used in the
            inference procedure.
        analyzed_gene_inds: numpy.ndarray of indices that denote which genes, as
            indexed in the original dataset, are (nonzero and) being used in the
            inference procedure.
        data: Loaded data as a dict, with ['matrix', 'barcodes', 'gene_names'].
        is_trimmed: This gets set to True after running
            trim_dataset_for_analysis().
        model_name: Name of model being run.
        transformation: Transformation applied to count data (and priors).
        priors: Priors estimated from the data useful for modelling.

    Note: Count data is kept as the original, untransformed data.  Priors are
    in terms of the transformed count data.

    """

    def __init__(self, transformation: trans.DataTransform = trans.IdentityTransform(),
                 input_file: Union[str, None] = None,
                 expected_cell_count: Union[int, None] = None,
                 num_transition_barcodes: Union[int, None] = None,
                 fraction_empties: float = 0.5,
                 model_name: str = None,
                 gene_blacklist: List[int] = [],
                 low_count_threshold: int = 30):
        super(Dataset, self).__init__()
        self.input_file = input_file
        self.analyzed_barcode_inds = np.array([])  # Barcodes trained each epoch
        self.analyzed_gene_inds = np.array([])
        self.empty_barcode_inds = np.array([])  # Barcodes randomized in training
        self.data = None
        self.model_name = model_name
        self.fraction_empties = fraction_empties
        self.is_trimmed = False
        self.transformation = transformation
        self.low_count_threshold = low_count_threshold
        self.priors = {'n_cells': expected_cell_count}

        # Load the dataset.
        self._load_data()

        # For an empty dataset object, skip the rest of initialization.
        if self.data is None:
            return

        # Estimate the number of real cells if it was not specified.
        if expected_cell_count is not None:
            self.priors['n_cells'] = expected_cell_count
        else:
            self.priors['n_cells'] = estimate_cell_count_from_dataset(self)

        # Set a default number of transition barcodes if not specified.
        if num_transition_barcodes is None:
            num_transition_barcodes = 7000

        # Trim the dataset.
        self._trim_dataset_for_analysis(num_transition_barcodes=num_transition_barcodes,
                                        low_UMI_count_cutoff=low_count_threshold,
                                        gene_blacklist=gene_blacklist)

        # Estimate priors.
        self._estimate_priors()

    def _load_data(self):
        """Load a dataset into the Dataset object from the self.input_file"""

        # This allows a simulated dataset to use the same constructor.
        if self.input_file is None:
            return

        logging.info(f"Loading data from file {self.input_file}")

        # Load the dataset.
        self.data = get_matrix_from_h5(self.input_file)

    def _trim_dataset_for_analysis(self,
                                   low_UMI_count_cutoff: int = 30,
                                   num_transition_barcodes: Union[int, None] = 7000,
                                   gene_blacklist: List[int] = []):
        """Trim the dataset for inference, choosing barcodes and genes to use.

        Sets the values of self.analyzed_barcode_inds, and
        self.empty_barcode_inds, which are used throughout training.

        Args:
            low_UMI_count_cutoff: Barcodes with total UMI counts below this
                number are excluded.
            num_transition_barcodes: Number of uncertain droplets to include
                during inference.
            gene_blacklist: List of gene indices to trim out and exclude.

        Note:
            self.priors['n_cells'] is only used to choose which barcodes to
            include in the analysis, as well as to estimate the sizes of cells
            and empty droplets (elsewhere).  It need only be a reasonable guess.
            The full analysis makes inferences about which barcodes contain
            cells, and ultimately will determine this number.
            However, if running only the 'simple' model, the expected_cell_count
            is taken to be the true number of cells, since empty droplets are
            not part of the 'simple' model.

        """

        logging.info("Trimming dataset for inference.")

        # Get data matrix and barcode order that sorts barcodes by UMI count.
        matrix = self.data['matrix']
        umi_counts = np.array(matrix.sum(axis=1)).squeeze()
        umi_count_order = np.argsort(umi_counts)[::-1]

        # Initially set the default to be the whole dataset.
        self.analyzed_barcode_inds = np.arange(start=0, stop=matrix.shape[0])
        self.analyzed_gene_inds = np.arange(start=0, stop=matrix.shape[1])

        # Expected cells must not exceed nonzero count barcodes.
        num_nonzero_barcodes = np.sum(umi_counts > 0).item()
        n_cells = min(self.priors['n_cells'], num_nonzero_barcodes)

        try:

            # Choose which genes to use based on their having nonzero counts.
            # (All barcodes must be included so that inference can generalize.)
            gene_counts_per_barcode = np.array(matrix.sum(axis=0)).squeeze()
            self.analyzed_gene_inds = np.where(gene_counts_per_barcode
                                               > 0)[0].astype(dtype=int)

            if len(gene_blacklist) > 0:

                # Ensure genes on the blacklist are excluded.
                self.analyzed_gene_inds = np.array([g for g in
                                                    self.analyzed_gene_inds
                                                    if g not in gene_blacklist])

        except IndexError:
            logging.warning("Something went wrong trying to trim genes.")

        # Estimate priors on cell size and 'empty' droplet size.
        self.priors['cell_counts'], self.priors['empty_counts'] = \
            get_d_priors_from_dataset(self)  # After gene trimming

        # If running the simple model, just use the expected cells, no more.
        if self.model_name == "simple":

            self.analyzed_barcode_inds = np.array(umi_count_order[:n_cells],
                                                  dtype=int)

        # If not using the simple model, include empty droplets.
        else:

            try:

                # Get the cell barcodes.
                cell_barcodes = umi_count_order[:n_cells]

                # Set the low UMI count cutoff to be the greater of either
                # the user input value, or an empirically-derived value.
                empirical_low_UMI = int(self.priors['empty_counts'] * 0.8)
                low_UMI_count_cutoff = max(low_UMI_count_cutoff,
                                           empirical_low_UMI)
                logging.info(f"Excluding barcodes with counts below "
                             f"{low_UMI_count_cutoff}")

                # See how many barcodes there are to work with total.
                num_barcodes_above_umi_cutoff = \
                    np.sum(umi_counts > low_UMI_count_cutoff).item()

                # Get a number of transition-region barcodes.
                num = min(num_transition_barcodes,
                          num_barcodes_above_umi_cutoff - cell_barcodes.size)
                num = max(0, num)
                transition_barcodes = umi_count_order[n_cells:
                                                      (n_cells + num)]

                # Use the cell barcodes and transition barcodes for analysis.
                self.analyzed_barcode_inds = np.concatenate((
                    cell_barcodes,
                    transition_barcodes)).astype(dtype=int)

                # Identify probable empty droplet barcodes.
                if num < num_transition_barcodes:

                    # This means we already used all the barcodes.
                    empty_droplet_barcodes = np.array([])

                else:

                    # Decide which empty barcodes to include.
                    empty_droplet_sorted_barcode_inds = \
                        np.arange(n_cells + num, num_barcodes_above_umi_cutoff,
                                  dtype=int)  # The entire range
                    # empty_droplet_sorted_barcode_inds = \
                    #     np.arange(n_cells + num,
                    #               min(num_barcodes_above_umi_cutoff
                    #                   - cell_barcodes.size - num,
                    #                   n_cells + num + num_empty_droplets),
                    #               dtype=int)
                    empty_droplet_barcodes = \
                        umi_count_order[empty_droplet_sorted_barcode_inds]

                self.empty_barcode_inds = empty_droplet_barcodes.astype(dtype=int)

                logging.info(f"Using {cell_barcodes.size} probable cell barcodes, "
                             f"plus an additional {transition_barcodes.size} barcodes, "
                             f"and {empty_droplet_barcodes.size} empty droplets.")

            except IndexError:
                logging.warning("Something went wrong trying to trim barcodes.")

        self.is_trimmed = True

    def _estimate_priors(self):
        """Estimate relevant priors, populating fields in the self.priors dict."""

        # Estimate the log UMI count turning point between cells and 'empties'.
        self.priors['log_counts_crossover'] = \
            np.mean(np.log1p([self.priors['cell_counts'],
                              self.priors['empty_counts']])).item()

        # Estimate prior for the scale param of LogNormal for d.
        if self.model_name != "simple":
            self.priors['d_std'] = (np.log1p(self.priors['cell_counts'])
                                    - self.priors['log_counts_crossover']) / 5
        else:
            self.priors['d_std'] = 0.2  # This is a reasonable prior in log space.

        # Priors for models that include empty droplets:
        if self.model_name != "simple":
            # Estimate fraction of trimmed dataset that contains cells.
            # cell_prob = self.priors['n_cells'] / self.analyzed_barcode_inds.size
            cell_prob = (1 - self.fraction_empties) \
                        * (self.priors['n_cells'] / self.analyzed_barcode_inds.size)
            self.priors['cell_prob'] = cell_prob

            assert cell_prob > 0, f"Fraction of trimmed dataset " \
                                  f"containing cells should be > 0, " \
                                  f"but is {cell_prob}."

            assert cell_prob <= 1, f"Fraction of trimmed dataset " \
                                   f"containing cells should be at most 1, " \
                                   f"but is {cell_prob}."

            # Turn cell probability into logit.
            self.priors['cell_logit'] = np.log(cell_prob / (1 - cell_prob)).item()

            # Estimate the ambient gene expression profile.
            self.priors['chi_ambient'], self.priors['chi_bar'] = \
                estimate_chi_from_dataset(self)

    def get_count_matrix(self) -> sp.csr.csr_matrix:
        """Get the count matrix, trimmed if trimming has occurred."""

        if self.is_trimmed:

            # Return the count matrix for selected barcodes and genes.
            trimmed_bc_matrix = self.data['matrix'][self.analyzed_barcode_inds,
                                                    :].tocsc()
            trimmed_matrix = trimmed_bc_matrix[:, self.analyzed_gene_inds].tocsr()

            # Apply transformation to the count data.
            return self.transformation.transform(trimmed_matrix)

        else:
            logging.warning("Using full count matrix, without any trimming.  "
                            "Could be slow.")

            # Apply transformation to the count data.
            return self.transformation.transform(self.data['matrix'])

    def get_count_matrix_empties(self) -> sp.csr.csr_matrix:
        """Get the count matrix for empty drops, trimmed if trimming has occurred."""

        if self.is_trimmed:

            # Return the count matrix for selected barcodes and genes.
            trimmed_bc_matrix = self.data['matrix'][self.empty_barcode_inds,
                                                    :].tocsc()
            trimmed_matrix = trimmed_bc_matrix[:, self.analyzed_gene_inds].tocsr()

            # Apply transformation to the count data.
            return self.transformation.transform(trimmed_matrix)

        else:
            logging.error("Trying to get empty count matrix without trimmed data.")

            # Apply transformation to the count data.
            return self.transformation.transform(self.data['matrix'])

    def get_count_matrix_all_barcodes(self) -> sp.csr.csr_matrix:
        """Get the count matrix, trimming only genes, not barcodes."""

        if self.is_trimmed:

            # Return the count matrix for selected barcodes and genes.
            trimmed_bc_matrix = self.data['matrix'].tocsc()
            trimmed_matrix = trimmed_bc_matrix[:, self.analyzed_gene_inds].tocsr()

            # Apply transformation to the count data.
            return self.transformation.transform(trimmed_matrix)

        else:
            logging.warning("Using full count matrix, without any trimming.  "
                            "Could be slow.")

            # Apply transformation to the count data.
            return self.transformation.transform(self.data['matrix'])

    def save_to_output_file(self,
                            output_file: str,
                            inferred_model,
                            save_plots: bool = False) -> bool:
        """Write the results of an inference procedure to an output file.

        Output is an HDF5 file.  To be written:
        Inferred ambient-subtracted UMI count matrix.
        Inferred probabilities that each barcode contains a real cell.
        Inferred cell size scale factors.
        Inferred ambient gene expression count vector for droplets without
            cells.
        Inferred contamination fraction hyperparameters.
        Embeddings of gene expression of cells into a low-dimensional latent
            space.

        Args:
            inferred_model: cellbender.model.VariationalInferenceModel which has
                already had the inference procedure run.
            output_file: Name of output .h5 file
            save_plots: Setting this to True will save plots of outputs.

        Returns:
            True if the output was written to file successfully.

        """

        logging.info("Preparing to write outputs to file...")

        # Calculate quantities of interest from the model.
        # Encoded values of latent variables.
        z, d, p = cellbender.remove_background.\
            model.get_encodings(inferred_model,
                                self,
                                cells_only=True)

        # Estimate the ambient-background-subtracted UMI count matrix.
        if self.model_name != "simple":

            inferred_count_matrix = \
                cellbender.remove_background.model.\
                    get_count_matrix_from_encodings(z, d, p,
                                                    inferred_model,
                                                    self,
                                                    cells_only=True)
        else:

            # No need to generate a new count matrix for simple model.
            inferred_count_matrix = self.data['matrix'].tocsc()
            logging.info("Simple model: outputting un-altered count matrix.")

        # TODO: add back in blacklisted genes: their original counts

        # Inferred ambient gene expression vector.
        ambient_expression_trimmed = cellbender.remove_background.model.\
            get_ambient_expression()

        # Convert the indices from trimmed gene set to original gene indices.
        ambient_expression = np.zeros(self.data['matrix'].shape[1])
        ambient_expression[self.analyzed_gene_inds] = ambient_expression_trimmed

        # Inferred contamination fraction hyperparameters.
        rho = cellbender.remove_background.model.get_contamination_fraction()

        # Inferred overdispersion hyperparameters.
        phi = cellbender.remove_background.model.get_overdispersion()

        # Figure out the indices of barcodes that have cells.
        if p is not None:
            p[np.isnan(p)] = 0.
            cell_barcode_inds = self.analyzed_barcode_inds
            assert np.sum(p > 0.5) > 0, "Found no cells."
            filtered_inds_of_analyzed_barcodes = p > 0.5
        else:
            cell_barcode_inds = self.analyzed_barcode_inds
            filtered_inds_of_analyzed_barcodes = np.arange(0, d.size)

        # If used, invert the transformation applied to input data.
        inferred_count_matrix = \
            self.transformation.inverse_transform(inferred_count_matrix)

        # Write to output file.
        write_succeeded = write_matrix_to_h5(output_file=output_file,
                                             gene_names=self.data['gene_names'],
                                             barcodes=self.data['barcodes'],
                                             inferred_count_matrix=
                                             inferred_count_matrix,
                                             cell_barcode_inds=cell_barcode_inds,
                                             ambient_expression=ambient_expression,
                                             rho=rho,
                                             phi=phi,
                                             z=z, d=d, p=p,
                                             loss=inferred_model.loss)

        # Generate filename for filtered matrix output.
        file_dir, file_base = os.path.split(output_file)
        file_name = os.path.splitext(os.path.basename(file_base))[0]
        filtered_output_file = os.path.join(file_dir,
                                            file_name + "_filtered.h5")

        # Write filtered matrix (cells only) to output file.
        if self.model_name != "simple":
            cell_barcode_inds = \
                self.analyzed_barcode_inds[filtered_inds_of_analyzed_barcodes]

            cell_barcodes = self.data['barcodes'][cell_barcode_inds]

            write_matrix_to_h5(output_file=filtered_output_file,
                               gene_names=self.data['gene_names'],
                               barcodes=cell_barcodes,
                               inferred_count_matrix=
                               inferred_count_matrix[cell_barcode_inds, :],
                               cell_barcode_inds=None,
                               ambient_expression=ambient_expression,
                               rho=rho,
                               phi=phi,
                               z=z[filtered_inds_of_analyzed_barcodes, :],
                               d=d[filtered_inds_of_analyzed_barcodes],
                               p=p[filtered_inds_of_analyzed_barcodes],
                               loss=inferred_model.loss)

            # Save barcodes determined to contain cells as _cell_barcodes.csv
            barcode_names = np.array([str(cell_barcodes[i], encoding='UTF-8')
                                     for i in range(len(cell_barcodes))])
            np.savetxt(os.path.join(file_dir, file_name + "_cell_barcodes.csv"),
                       barcode_names, delimiter=',', fmt='%s')

        try:
            # Save plots, if called for.
            if save_plots:
                plt.figure(figsize=(6, 18))

                # Plot the train and test error.
                plt.subplot(3, 1, 1)
                plt.plot(inferred_model.loss['train']['elbo'], '.--')
                plt.plot(inferred_model.loss['test']['epoch'],
                         inferred_model.loss['test']['elbo'], 'o:')
                plt.gca().set_ylim(bottom=max(inferred_model.loss['train']['elbo'][0],
                                              inferred_model.loss['train']['elbo'][-1] - 2000))
                plt.legend(['Train', 'Test'])
                plt.xlabel('Epoch')
                plt.ylabel('ELBO')
                plt.title('Progress of the training procedure')

                # Plot the barcodes used, along with the inferred cell probabilities.
                plt.subplot(3, 1, 2)
                count_mat = self.get_count_matrix()
                counts = np.array(count_mat.sum(axis=1)).squeeze()
                count_order = np.argsort(counts)[::-1]
                plt.semilogy(counts[count_order], color='black')
                plt.ylabel('UMI counts')
                plt.xlabel('Barcode index, sorted by UMI count')
                if p is not None:  # The case of a simple model.
                    plt.gca().twinx()
                    plt.plot(p[count_order], '.:', color='red', alpha=0.3)
                    plt.ylabel('Cell probability', color='red')
                    plt.ylim([-0.05, 1.05])
                    plt.title('Determination of which barcodes contain cells')
                else:
                    plt.title('The subset of barcodes used for training')

                # Plot the latent encoding via PCA.
                plt.subplot(3, 1, 3)
                pca = PCA(n_components=2)
                if p is None:
                    p = np.ones_like(d)
                z_pca = pca.fit_transform(z[p >= 0.5])
                plt.plot(z_pca[:, 0], z_pca[:, 1],
                         '.', ms=3, color='black', alpha=0.3)
                plt.ylabel('PC 1')
                plt.xlabel('PC 0')
                plt.title('PCA of latent encoding of cell gene expression')

                file_dir, file_base = os.path.split(output_file)
                file_name = os.path.splitext(os.path.basename(file_base))[0]
                fig_name = os.path.join(file_dir, file_name + ".pdf")
                plt.savefig(fig_name, bbox_inches='tight', format='pdf')
                logging.info(f"Saved summary plots as {fig_name}")

        except Exception:
            logging.warning("Unable to save plot.")

        return write_succeeded


def get_matrix_from_h5(filename: str) -> Dict[str,
                                              Union[sp.csr.csr_matrix,
                                                    List[np.ndarray],
                                                    np.ndarray]]:
    """Load a count matrix from an h5 file from CellRanger's output.

    The file needs to be a _raw_gene_bc_matrices_h5.h5 file.  This function
    returns a dictionary that includes the count matrix, the gene names (which
    correspond to columns of the count matrix), and the barcodes (which
    correspond to rows of the count matrix).

    Args:
        filename: string path to .h5 file that contains the raw gene
            barcode matrices

    Returns:
        out['matrix']: scipy.sparse.csr.csr_matrix of unique UMI counts, with
            barcodes as rows and genes as columns
        out['barcodes']: numpy array of strings which are the nucleotide
            sequences of the barcodes that correspond to the rows in
            the out['matrix']
        out['gene_names']: List of numpy arrays, where the number of elements
            in the list is the number of genomes in the dataset.  Each numpy
            array contains the string names of genes in the genome, which
            correspond to the columns in the out['matrix'].

    """

    # try:
    with tables.open_file(filename, 'r') as f:
        # Initialize empty lists.
        gene_names = []
        csc_list = []
        barcodes = None

        # Each group in the table (other than root) contains a genome,
        # so walk through the groups to get data for each genome.
        for group in f.walk_groups():
            try:
                # Read in data for this genome, and put it into a
                # scipy.sparse.csc.csc_matrix
                gene_names.extend(getattr(group, 'gene_names').read())
                barcodes = getattr(group, 'barcodes').read()
                data = getattr(group, 'data').read()
                indices = getattr(group, 'indices').read()
                indptr = getattr(group, 'indptr').read()
                shape = getattr(group, 'shape').read()
                csc_list.append(sp.csc_matrix((data, indices, indptr),
                                              shape=shape))
            except tables.NoSuchNodeError:
                # This exists to bypass the root node, which has no data.
                pass

    # Put the data from all genomes together.
    count_matrix = sp.vstack(csc_list, format='csc')
    count_matrix = count_matrix.transpose().tocsr()

    # Issue warnings if necessary, based on dimensions matching.
    if count_matrix.shape[1] != len(gene_names):
        logging.warning(f"Number of gene names in {filename} does not match "
                        f"the number expected from the count matrix.")
    if count_matrix.shape[0] != len(barcodes):
        logging.warning(f"Number of barcodes in {filename} does not match "
                        f"the number expected from the count matrix.")

    return {'matrix': count_matrix,
            'gene_names': np.array(gene_names),
            'barcodes': np.array(barcodes)}

    # In order to batch files, this exception is now caught in command_line.py
    # except IOError:
    #
    #     # Cannot read input file.  Terminate.
    #     logging.error("Unable to open file '%s'" % filename)
    #     sys.exit(IOError)


def write_matrix_to_h5(output_file: str,
                       gene_names: np.ndarray,
                       barcodes: np.ndarray,
                       inferred_count_matrix: sp.csc.csc_matrix,
                       cell_barcode_inds: Union[np.ndarray, None] = None,
                       ambient_expression: Union[np.ndarray, None] = None,
                       rho: Union[np.ndarray, None] = None,
                       phi: Union[np.ndarray, None] = None,
                       z: Union[np.ndarray, None] = None,
                       d: Union[np.ndarray, None] = None,
                       p: Union[np.ndarray, None] = None,
                       loss: Union[Dict, None] = None) -> bool:
    """Write count matrix data to output HDF5 file using CellRanger format.

    Args:
        output_file: Path to output .h5 file (e.g., 'output.h5').
        gene_names: Name of each gene (column of count matrix).
        barcodes: Name of each barcode (row of count matrix).
        inferred_count_matrix: Count matrix to be written to file, in sparse
            format.  Rows are barcodes, columns are genes.
        cell_barcode_inds: Indices into the original cell barcode array that
            were found to contain cells.
        ambient_expression: Vector of gene expression of the ambient RNA
            background counts that contaminate cell counts.
        rho: Hyperparameters for the contamination fraction distribution.
        phi: Hyperparameters for the overdispersion distribution.
        z: Latent encoding of gene expression.
        d: Latent encoding of cell size scale factor.
        p: Latent encoding of the probability that a barcode contains a cell.
        loss: Training and test error, as ELBO, for each epoch.

    Note:
        To match the CellRanger .h5 files, the matrix is stored as its
        transpose, with rows as genes and cell barcodes as columns.

    """

    assert isinstance(inferred_count_matrix,
                      sp.csc_matrix), "The count matrix must be csc_matrix " \
                                      "format in order to write to HDF5."

    assert gene_names.size == inferred_count_matrix.shape[1], \
        "The number of gene names must match the number of columns in the count" \
        "matrix."

    assert barcodes.size == inferred_count_matrix.shape[0], \
        "The number of barcodes must match the number of rows in the count" \
        "matrix."

    # This reverses the role of rows and columns, to match CellRanger format.
    inferred_count_matrix = inferred_count_matrix.transpose().tocsc()

    # Write to output file.
    try:
        with tables.open_file(output_file, "w",
                              title="Background-subtracted UMI counts") as f:

            # Create the group where data will be stored.
            group = f.create_group("/", "background_removed",
                                   "Counts after background correction")

            # Create arrays within that group for barcodes and gene_names.
            f.create_array(group, "gene_names", gene_names)
            f.create_array(group, "genes", np.arange(gene_names.size))  # For compatibility, added post PR
            f.create_array(group, "barcodes", barcodes)

            # Create arrays to store the count data.
            f.create_array(group, "data", inferred_count_matrix.data)
            f.create_array(group, "indices", inferred_count_matrix.indices)
            f.create_array(group, "indptr", inferred_count_matrix.indptr)
            f.create_array(group, "shape", inferred_count_matrix.shape)

            # Store background gene expression, barcode_inds, z, d, and p.
            if cell_barcode_inds is not None:
                f.create_array(group, "barcode_indices_for_latents",
                               cell_barcode_inds)
            if ambient_expression is not None:
                f.create_array(group, "ambient_expression", ambient_expression)
            if z is not None:
                f.create_array(group, "latent_gene_encoding", z)
            if d is not None:
                f.create_array(group, "latent_scale", d)
            if p is not None:
                f.create_array(group, "latent_cell_probability", p)
            if rho is not None:
                f.create_array(group, "contamination_fraction_params", rho)
            if phi is not None:
                f.create_array(group, "overdispersion_params", phi)
            if loss is not None:
                f.create_array(group, "training_elbo_per_epoch",
                               np.array(loss['train']['elbo']))

        logging.info(f"Succeeded in writing output to file {output_file}")

        return True

    except Exception:
        logging.warning(f"Encountered an error writing output to file "
                        f"{output_file}.  "
                        "Output may be incomplete.")

        return False


def get_d_priors_from_dataset(dataset: Dataset) -> Tuple[float, float]:
    """Compute an estimate of reasonable priors on cell size and ambient size.

    Given a dataset (scipy.sparse.csr matrix of counts where
    rows are barcodes and columns are genes), and an expected
    cell count, compute an estimate of reasonable priors on cell size
    and ambient count size.  This is done by a series of heuristics.

    Args:
        dataset: Dataset object containing a matrix of unique UMI counts,
            where rows are barcodes and columns are genes.

    Returns:
        cell_counts: Estimated mean number of UMI counts per real cell, in
            terms of transformed count data.
        empty_counts: Estimated mean number of UMI counts per 'empty'
            droplet, in terms of transformed count data.

    NOTE: Picks barcodes using cutoffs in untransformed count data.  The output
    is in terms of transformed counts.

    """

    # Count the total unique UMIs per barcode (summing after transforming).
    transformed_counts = \
        np.array(dataset.transformation.transform(dataset.data['matrix']
                                                  [:, dataset.analyzed_gene_inds])
                 .sum(axis=1)).squeeze()
    counts = np.array(dataset.data['matrix']
                      [:, dataset.analyzed_gene_inds].sum(axis=1)).squeeze()

    # If it's a model that does not model empty droplets, the dataset is cells.
    if dataset.model_name == 'simple':

        assert type(dataset.priors['n_cells']) is int, "No prior on number of cells."

        # Sort order the cells by counts.
        sort_order = np.argsort(counts)[::-1]

        # Estimate cell count by median, taking 'cells' to be the largest counts.
        cell_counts = int(np.median(transformed_counts[sort_order]
                                    [:dataset.priors['n_cells']]).item())

        empty_counts = 0

    # Models that include both cells and empty droplets.
    else:

        # Cutoff for original data.  Empirical.
        cut = dataset.low_count_threshold

        # Estimate the number of UMI counts in empty droplets.

        # Mode of (rounded) log counts (for counts > cut) is a robust empty estimator.
        empty_log_counts = mode(np.round(np.log1p(transformed_counts[counts > cut]),
                                         decimals=1))[0]
        empty_counts = int(np.expm1(empty_log_counts).item())

        # Estimate the number of UMI counts in cells.

        # Median of log counts above 5 * empty counts is a robust cell estimator.
        cell_log_counts = np.median(np.log1p(transformed_counts
                                             [transformed_counts > 5 * empty_counts]))
        cell_counts = int(np.expm1(cell_log_counts).item())

        logging.info(f"Prior on counts in empty droplets is {empty_counts}")

    logging.info(f"Prior on counts for cells is {cell_counts}")

    return cell_counts, empty_counts


def estimate_cell_count_from_dataset(dataset: Dataset) -> int:
    """Compute an estimate of number of real cells in a dataset.

    Given a Dataset, compute an estimate of the number of real cells.
    This is done CellRanger-style, by taking barcode with total UMI count
    in the 99th percentile of the dataset, and then finding the number of
    barcodes that have greater than 0.9 * that number of UMIs.

    Args:
        dataset: Dataset object containing a matrix of unique UMI counts,
            where rows are barcodes and columns are genes.

    Returns:
        cell_count_est: Estimated number of real cells.

    NOTE: This works best on un-transformed count data.

    TODO: make this more robust.

    """

    # If it's a model that does not model empty droplets, the dataset is cells.
    # NOTE: this is overridden if --expected_cells is specified.
    if dataset.model_name == 'simple':
        return dataset.data['matrix'].shape[0]

    # Count number of UMIs in each barcode.
    counts = np.array(dataset.data['matrix'].sum(axis=1),
                      dtype=int).squeeze()

    # Find the order that sorts barcodes by UMI count.
    count_sort_order = np.argsort(counts)[::-1]  # Decreasing UMI counts

    # Find the UMI count cutoff as 0.9 * counts(99th percentile barcode)
    ninety_ninth_percentile_ind = int(counts.size * 0.01)
    umi_cutoff = 0.9 * counts[count_sort_order][ninety_ninth_percentile_ind]

    # Count the number of barcodes with UMI counts above the cutoff.
    cell_count_est = int(np.sum(counts > umi_cutoff).item())

    return cell_count_est


def estimate_chi_from_dataset(dataset: Dataset) -> Tuple[torch.Tensor,
                                                         torch.Tensor]:
    """Compute an estimate of ambient RNA levels.

    Given a Dataset, compute an estimate of the ambient gene expression and
    compute the average gene expression.

    Args:
        dataset: Dataset object containing a matrix of unique UMI counts,
            where rows are barcodes and columns are genes.

    Returns:
        chi_ambient_init: Estimated number of real cells.
        chi_bar: Average gene expression over dataset.

    NOTE: This must be done on transformed data.

    """

    # Ensure that an estimate of the log count crossover point between cells
    # and empty droplets has already been calculated.
    try:
        log_crossover = dataset.priors['log_counts_crossover']
    except KeyError:
        raise AssertionError("Could not find dataset parameter "
                             "log_counts_crossover.")

    ep = np.finfo(np.float32).eps.item()  # Small value

    # Trimmed and appropriately transformed count matrix.
    count_matrix = dataset.get_count_matrix()

    # Empty droplets have log counts < log_crossover.
    empty_barcodes = (np.log(np.array(count_matrix.sum(axis=1)).squeeze())
                      < log_crossover)

    # Sum gene expression for the empty droplets.
    gene_expression = np.array(count_matrix[empty_barcodes, :].sum(axis=0)).squeeze()

    # As a vector on a simplex.
    gene_expression = gene_expression + ep
    chi_ambient_init = \
        torch.Tensor(gene_expression / np.sum(gene_expression))

    # Full count matrix, appropriately transformed.
    full_count_matrix = dataset.get_count_matrix_all_barcodes()

    # Sum all gene expression.
    gene_expression_total = np.array(full_count_matrix.sum(axis=0)).squeeze()

    # As a vector on a simplex.
    gene_expression_total = gene_expression_total + ep
    chi_bar = \
        torch.Tensor(gene_expression_total / np.sum(gene_expression_total))

    return chi_ambient_init, chi_bar
