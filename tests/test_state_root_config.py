from pathlib import Path

from pico.state_root import default_state_root


def test_explicit_state_root_keeps_runtime_data_on_selected_drive():
    configured = r"E:\pico-runtime-state"

    assert default_state_root({"PICO_STATE_ROOT": configured}) == Path(configured)
