"""
Here are the post_save and the pre_delete signals
"""
from django.db import router
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


def manual_save(model, ids, action: str = 'edit'):
    """Ручная вставка в таблицу для индексации
       :param model: модель
       :param ids: список идентификаторов
       :param action: действие (add/edit/delete)
    """
    if not model or not ids or action not in ('add', 'edit', 'delete'):
        return
    # Узнать базу для чтения модели model
    read_db = router.db_for_read(model)
    # Узнать базу для записи модели Change
    write_db = router.db_for_write(Change)

    changes = []
    for item in model.objects.using(read_db).filter(pk__in=ids):
        item._state.db = write_db # Меняем бд на ту, где Change
        changes.append(Change(object=item, action=action))
    if changes:
        Change.objects.bulk_create(changes, ignore_conflicts=True)
