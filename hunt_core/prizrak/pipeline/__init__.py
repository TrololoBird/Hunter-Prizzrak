"""Surviving low-level primitive from the retired 5-module gate.

Only ``structure.py::_detect_structure`` (reused directly by
``hunt_core.prizrak.structure``) survives. Everything else — the gating
orchestration, macro veto-gate, trend/positioning/risk/vp_ofi/oi_rank modules,
the ``ModuleResult``/``FiveModuleResult`` result types (``types.py``), and the
unwired ``run_structure_module`` — was deleted: the PrizrakTrade engine
(``hunt_core.prizrak.orchestrator``) is the sole decision authority and does not
use them.
"""
