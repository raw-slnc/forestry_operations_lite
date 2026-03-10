def classFactory(iface):  # pylint: disable=invalid-name
    """Load ForestryOperationsLite class."""
    from .forestry_operations_lite import ForestryOperationsLite
    return ForestryOperationsLite(iface)
