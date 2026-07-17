"""Hand-written SQL modules, one per table (§1).

Every function takes an open connection and assumes the caller established
tenant context (`Database.with_tenant`) — these never open transactions of
their own, so callers stay in control of transaction boundaries (§5.4).
"""
