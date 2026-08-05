import os
import importlib
import traceback

from django.utils.module_loading import import_module


class NoModuleError(Exception):
    """
    Custom exception class indicates that given module does not exit at all
    """
    pass


def get_module(base, module_name: str = 'index'):
    """Load module over importlib.import_module('apps.product.index')
       :param base: app label (apps.product)
       :param module_name: file name (models_abstract)
    """
    try:
        base_path = __import__(base, {}, {}, [base.split('.')[-1]]).__path__
    except AttributeError:
        raise NoModuleError('Can not load base `%s`' % base)

    try:
        importlib.import_module('%s.%s' % (base, module_name)) # apps.product.index
    except ImportError as e:
        err = 'Can not find module `%s.%s` %s' % (base, module_name, e)
        if base_path:
            module_path = '%s/%s.py' % (base_path[0], module_name)
            if os.path.exists(module_path):
                traceback.print_exc()
        raise NoModuleError(err)

    result = import_module('.%s' % module_name, base)
    return result
