"""The shared tenant execution loop.

`core.runtime.TenantRunner` is the loop every model runs; `TenantStrategy` is
the surface a model implements. See `core/runtime/tenant.py`.
"""
from core.runtime.tenant import TenantContext, TenantRunner, TenantStrategy

__all__ = ["TenantContext", "TenantRunner", "TenantStrategy"]
