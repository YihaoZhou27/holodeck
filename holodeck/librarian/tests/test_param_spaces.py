"""
"""

import numpy as np

import holodeck as holo
from holodeck import librarian


def _run_param_space(param_space_class):
    NSAMPLES = 10
    SAM_SHAPE = 13
    pspace = param_space_class(holo.log, nsamples=NSAMPLES, sam_shape=SAM_SHAPE)
    sam, hard = pspace.model_for_sample_number(0)
    # Make sure model runs
    import holodeck.librarian.libraries  # noqa
    data = librarian.libraries.run_model(sam, hard, singles_flag=True, details_flag=True)
    assert data is not None, "After `run_model` returned data is None!"
    check_keys = ['fobs_cents', 'fobs_edges', 'hc_ss', 'hc_bg', 'gwb', 'static_binary_density', 'number']
    for key in check_keys:
        assert key in data, f"After `run_model` returned data does not have key '{key}'!  ({data.keys()=})"
        vals = data[key]
        assert np.any(vals > 0.0), f"After `run_model`, no positive values found for key '{key}'!"

    return


def test_all_param_spaces():

    print("test_all_param_spaces()")
    for kk, vv in librarian.param_spaces_dict.items():
        print(f"==== Testing param space   {kk}")
        _run_param_space(vv)

    return

