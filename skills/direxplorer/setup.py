"""
DirExplorer skill install/uninstall script.
"""
import os


def install(context: dict) -> dict:
    return {'success': True, 'message': 'DirExplorer skill installed successfully.'}


def uninstall(context: dict) -> dict:
    return {'success': True, 'message': 'DirExplorer skill uninstalled successfully.'}
