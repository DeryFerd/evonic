"""
Explorer skill install/uninstall script.
"""


def install(context: dict) -> dict:
    return {'success': True, 'message': 'Explorer skill installed successfully.'}


def uninstall(context: dict) -> dict:
    return {'success': True, 'message': 'Explorer skill uninstalled successfully.'}
