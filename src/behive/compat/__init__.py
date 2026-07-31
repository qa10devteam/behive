"""
BeHive legacy import compatibility layer.

The engine modules were originally named hive2_*.py (flat files).
This package provides shims so existing import statements work
without modification.

Example: 'from behive.engine.drones import get_strategy' resolves to
         'from behive.engine.drones import get_strategy'
"""
