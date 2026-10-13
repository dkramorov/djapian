"""
Here are the post_save and the pre_delete signals
"""
from djapian.models import Change


def get_model_manager(**kwargs):
    """Получем менеджер для модели,
       с учетом бд
       в **kwargs аргумент using — алиас базы данных, в которую была произведена запись
       в db router должно быть разрешение писать во все базы для contenttypes
       Индексация итак будет происходить правильно,
       т/к object manager будет выбирать записи согласно db router
    """
    db_from_using = kwargs.get('using')
    manager = Change.objects
    # Пока не используем разные бд 'contenttypes' не должно быть в db router, в этом случае
    #if db_from_using:
    #    manager = manager.using(db_from_using)
    return manager


def post_save(sender, instance, created, *args, **kwargs):
    """Create the Change object to update the index
    """
    Change.objects.create(object=instance, action=created and 'add' or 'edit')



def pre_delete(sender, instance, *args, **kwargs):
    """Create the Change object to update the index
    """
    Change.objects.create(object=instance, action='delete')
