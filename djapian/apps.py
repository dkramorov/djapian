from django.apps import AppConfig


class DjapianConfig(AppConfig):
    """Djapian - индексный поиск xapian
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'djapian'
    verbose_name = 'Индексный поиск'
