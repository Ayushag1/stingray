
import numpy as np
import os
import matplotlib.pyplot as plt

from astropy.tests.helper import pytest

from ..io import split_numbers
from ..io import ref_mjd
from ..io import high_precision_keyword_read
from ..io import load_events_and_gtis, read_mission_info
from ..io import read_header_key

import warnings

curdir = os.path.abspath(os.path.dirname(__file__))
datadir = os.path.join(curdir, 'data')

_H5PY_INSTALLED = True

try:
    import h5py
except ImportError:
    _H5PY_INSTALLED = False

skip_condition = pytest.mark.skipif(not _H5PY_INSTALLED,
    reason = "H5PY not installed.")


class TestIO(object):

    def test_common_name(self):
        """Test the common_name function."""
        from ..io import common_name
        a = 'A_3-50_A'
        b = 'B_3-50_B'
        assert common_name(a, b) == '3-50'

    def test_high_precision_keyword(self):
        """Test high precision FITS keyword read."""
        hdr = {"MJDTESTI": 100, "MJDTESTF": np.longdouble(0.5),
               "CIAO": np.longdouble(0.)}
        assert \
            high_precision_keyword_read(hdr,
                                        "MJDTEST") == np.longdouble(100.5), \
            "Keyword MJDTEST read incorrectly"
        assert \
            high_precision_keyword_read(hdr,
                                        "MJDTESTA") == np.longdouble(100.5), \
            "Keyword MJDTESTA read incorrectly"
        assert \
            high_precision_keyword_read(hdr, "CIAO") == np.longdouble(0.), \
            "Keyword CIAO read incorrectly"
        assert high_precision_keyword_read(hdr, "BU") is None, \
            "Inexistent key read incorrectly"

    def test_xselect_mdb_is_found_headas(self, monkeypatch, tmp_path):
        """Test event file reading."""
        path = tmp_path / 'bin'
        path.mkdir()
        f = path / 'xselect.mdb'
        f.write_text("MAXI:submkey       NONE\nMAXI:instkey       INSTRUME")

        monkeypatch.setenv("HEADAS", tmp_path)

        info = read_mission_info()
        assert "NUSTAR" not in info

    def test_read_whole_mission_info(self):
        """Test event file reading."""
        info = read_mission_info()
        assert "NUSTAR" in info
        assert "XMM" in info
        assert "NICER" in info

    def test_event_file_read(self):
        """Test event file reading."""
        fname = os.path.join(datadir, 'monol_testA.evt')
        load_events_and_gtis(fname, additional_columns=["PI"])

    def test_event_file_read_additional_warns_uncal(self):
        """Test event file reading."""
        fname = os.path.join(datadir, 'monol_testA.evt')
        with pytest.warns(UserWarning) as record:
            vals = load_events_and_gtis(fname, additional_columns=["energy"])
        assert np.any(["Column energy not found"
                       in r.message.args[0] for r in record])
        # This is the default calibration for nustar data, as returned
        # from rough_calibration
        assert np.allclose(vals.energy_list, vals.pi_list * 0.04 + 1.6)

    def test_event_file_read_additional_energy_cal(self):
        """Test event file reading."""
        fname = os.path.join(datadir, 'monol_testA_calib.evt')
        vals = load_events_and_gtis(fname, additional_columns=["energy"])
        # These energies were calibrated with a different calibration than
        # returned from rough_calibration, on purpose! (notice the +1.)
        assert np.allclose(vals.energy_list, vals.pi_list * 0.04 + 1.6 + 1.)

    def test_event_file_read_xmm(self):
        """Test event file reading."""
        fname = os.path.join(datadir, 'xmm_test.fits')
        with pytest.warns(UserWarning) as record:
            load_events_and_gtis(fname, additional_columns=['PRIOR'])
        assert np.any(["Trying first extension"
                in r.message.args[0] for r in record])

    def test_event_file_read_no_mission(self):
        """Test event file reading."""
        fname = os.path.join(datadir, 'nomission.evt')
        load_events_and_gtis(fname)

    def test_event_file_read_no_additional(self):
        """Test event file reading."""
        fname = os.path.join(datadir, 'monol_testA.evt')
        load_events_and_gtis(fname)

    def test_event_file_read_no_pi(self):
        """Test event file reading."""
        fname = os.path.join(datadir, 'monol_testA.evt')
        load_events_and_gtis(fname)

    def test_read_header_key(self):
        """Test event file reading."""
        fname = os.path.join(datadir, 'monol_testA.evt')
        assert read_header_key(fname, "INSTRUME") == 'FPMA'
        assert read_header_key(fname, "BU") == ""

    def test_read_mjdref(self):
        """Test event file reading."""
        fname = os.path.join(datadir, 'monol_testA.evt')
        assert ref_mjd(fname) is not None

    def test_split_number(self):
        """Test split with high precision numbers."""
        numbers = np.array([57401.0000003423423400453453,
            0.00000574010000003426646], dtype = np.longdouble)
        number_I, number_F = split_numbers(numbers)
        r_numbers = np.longdouble(number_I) + np.longdouble(number_F)

        assert (numbers == r_numbers).all()

        n = [1234.567, 12.345]
        shift = -2
        n_i, n_f = split_numbers(n, shift)
        assert np.allclose(n_i, [1200, 0])
        r_n = (n_i + n_f)
        assert (n == r_n).all()

class TmpIOReadWrite(object):
    """A temporary helper class to test all the read and write functions."""
    def __init__(self):
        self.number = 10
        self.str = 'Test'
        self.list = [1,2,3]
        self.array = np.array([1,2,3])
        self.long_number = np.longdouble(1.25)
        self.long_array = np.longdouble([1,2,3])

    def test_operation(self):
        return self.number * 10


class TestFileFormats(object):

    def test_savefig_without_plot(self):
        from ..io import savefig
        plt.close("all")
        with warnings.catch_warnings(record=True) as w:
            savefig('test.png')
            assert np.any(["plot the image first" in str(wi.message) for wi in w])
        os.unlink('test.png')

    def test_savefig(self):
        from ..io import savefig
        plt.plot([1, 2, 3])
        savefig("test.png")
        os.unlink("test.png")
