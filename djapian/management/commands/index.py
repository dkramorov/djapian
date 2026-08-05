import os
import sys
import operator
import time
import shutil
from datetime import datetime
from optparse import make_option
from functools import reduce

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ImproperlyConfigured
from django.db import models, transaction
from django.contrib.contenttypes.models import ContentType

from djapian.models import Change
from djapian import utils
from djapian.utils.paging import paginate
from djapian.utils.commiter import Commiter
from djapian.space import IndexSpace
from djapian.database import Database


def get_content_types(app_models, *actions):
    lookup_args = dict(action__in=actions)
    if app_models is not None:
        ct_list = [ContentType.objects.get_for_model(model) for model in app_models]
        lookup_args.update(dict(content_type__in=ct_list))
    types = Change.objects.filter(models.Q(**lookup_args))\
                    .values_list('content_type', flat=True)\
                    .distinct()
    return ContentType.objects.filter(pk__in=types)


def get_indexers(content_type):
    return reduce(
        operator.add,
        [space.get_indexers_for_model(content_type.model_class())
            for space in IndexSpace.instances]
    )


#@transaction.atomic
def update_changes(verbose, timeout, per_page, commit_each, app_models=None):
    counter = [0]

    def reset_counter():
        counter[0] = 0

    def after_index(obj):
        counter[0] += 1

        if verbose:
            sys.stdout.write('.')
            sys.stdout.flush()

    commiter = Commiter.create(commit_each)(
        lambda: None,
        transaction.commit,
        transaction.rollback
    )

    while True:
        count = Change.objects.count()
        if count > 0 and verbose:
            print('There are %d objects to update' % count)

        for ct in get_content_types(app_models, 'add', 'edit'):
            indexers = get_indexers(ct)

            for page in paginate(
                            Change.objects.filter(content_type=ct, action__in=('add', 'edit'))\
                                .select_related('content_type')\
                                .order_by('object_id'),
                            per_page
                        ): # The objects must be sorted by date

                commiter.begin_page()

                try:
                    for indexer in indexers:
                        indexer.update(
                            ct.model_class()._default_manager.filter(
                                pk__in=[c.object_id for c in page.object_list]
                            ).order_by('pk'),
                            after_index,
                            per_page,
                            commit_each
                        )

                    for change in page.object_list:
                        change.delete()

                    commiter.commit_page()
                except Exception:
                    if commit_each:
                        for change in page.object_list[:counter[0]]:
                            change.delete()
                        commiter.commit_object()
                    else:
                        commiter.cancel_page()
                    raise

                reset_counter()

        for ct in get_content_types(app_models, 'delete'):
            indexers = get_indexers(ct)

            for change in Change.objects.filter(content_type=ct, action='delete'):
                for indexer in indexers:
                    indexer.delete(change.object_id)
                change.delete()

        # If using transactions and running Djapian as a daemon, transactions
        # need to be committed on each iteration, otherwise Djapian will not
        # catch changes. We also need to use the commit_manually decorator.
        #
        # Background information:
        #
        # Autocommit is turned off by default according to PEP 249.
        # PEP 249 states "Database modules that do not support transactions
        #                 should implement this method with void functionality".
        # Consistent Nonlocking Reads (InnoDB):
        # http://dev.mysql.com/doc/refman/5.0/en/innodb-consistent-read-example.html
        transaction.commit()
        break
        time.sleep(timeout)


objects_counter = 0
total_objects = 0
rebuild_started = 0


def rebuild(verbose, per_page, commit_each, app_models=None):
    global objects_counter
    global total_objects
    global rebuild_started

    if verbose:
        print("rebuild started at %s" % datetime.today().strftime('%H:%M %d/%m/%Y'))

    def after_index(obj):
        global objects_counter
        global total_objects
        global rebuild_started

        objects_counter += 1
        if verbose:
          if objects_counter % 50000 == 0 and objects_counter > 0:
            elapsed = time.time()-rebuild_started
            print(unicode(obj).split(".")[-1], objects_counter, "/", total_objects, "=>", "%.0f sec (%.1f min)" % (elapsed, elapsed/60))
          if not total_objects:
            total_objects = obj.__class__.objects.all().aggregate(models.Count("id"))['id__count']
        #if verbose:
            #sys.stdout.write('.')
            #sys.stdout.flush()

    rebuild_started = time.time()
    for space in IndexSpace.instances:
        for model, indexers in space.get_indexers().iteritems():
            if app_models is None or model in app_models:
                indexer_started = time.time()
                for indexer in indexers:
                    indexer_started = time.time()

                    indexer.clear()
                    indexer.update(None, after_index, per_page, commit_each)

                    if verbose:
                      elapsed = time.time() - indexer_started
                      total_elapsed = time.time() - rebuild_started
                      print(unicode(indexer).split(".")[-1], "=> %.0f sec (%.1f min)" % (elapsed, elapsed/60))
                      objects_counter = 0
                      total_objects = 0

    seconds = time.time()-rebuild_started
    print("rebuild took %.0f sec (%.1f min)" % (seconds, seconds/60))


def rebuild_index(indexer: str = None, model = None, verbose: bool = False):
    """Полный ребилд индекса
       :param indexer: ребилд по индексу
       :param model: ребилд по модели
       :param verbose: режим дополнительного информирования
    """
    Change.objects.all().delete()
    # ----------------------------------------------
    # Создаем альтернативную базу по всем индексам,
    # индексируем ее,
    # затем помещаем на место основной базы индексов
    # ----------------------------------------------
    djapian_folder = settings.DJAPIAN_DATABASE_PATH
    tmp_djapian_folder = os.path.join(djapian_folder, 'TMP_DJAPIAN')
    if verbose:
        print('--------------------')
        print('indexer            :', indexer)
        print('model              :', model)
        print('djapian_folder     :', djapian_folder)
        print('tmp_djapian_folder :', tmp_djapian_folder)
        print('--------------------')
    for item in IndexSpace.instances:
        for m, indexers in item.get_indexers().items():
            if verbose:
                print(m, indexers)
            # -----------------------------
            # Работаем с конкретной моделью
            # -----------------------------
            if model and not m == model:
                continue
            # ------------------------------
            # Работаем с конкретным индексом
            # ------------------------------
            for ind in indexers:
                if indexer and not ind == indexer:
                    continue

                indexer_started = time.time()
                old_db_path = ind._db._path
                new_db_path = old_db_path.replace(djapian_folder, tmp_djapian_folder)
                new_db = Database(new_db_path)
                ind._db = new_db
                ind.clear()
                ind.update(None, None, 10000, False)
                indexer_elapsed = time.time() - indexer_started
                print(ind, 'elapsed %.2f sec (%.2f min)' % (indexer_elapsed, indexer_elapsed/60))

                tmp = old_db_path + '_old'
                try:
                    shutil.rmtree(tmp)
                except Exception as e:
                    print('[ERROR]: folder not dropped', e)
                try:
                    shutil.move(old_db_path, tmp)
                except Exception as e:
                    print('[ERROR]: folder not moved', e)
                try:
                    shutil.move(new_db_path, old_db_path)
                    print('[ALERT]: folder replaced', old_db_path)
                except Exception as e:
                    print('[ERROR] folder not replaced', e)
                try:
                    shutil.rmtree(tmp)
                except Exception as e:
                    print('[ERROR]: folder not dropped', e)


def get_indexer_and_model(indexer: str = '', model: str = '', verbose: bool = False):
    """Возвращает конкретный индексер и/или модель для индексации
       :param indexer: индекс по которому хотим поработать
       :param model: модель по которой хотим поработать
       :param verbose: режим дополнительного информирования
    """
    # -------------------------------------
    # Если хотим задать какой-то конкретный
    # индекс или модель для переиндексации
    # -------------------------------------
    index_indexer = None
    index_model = None
    for item in IndexSpace.instances:
        for m, indexers in item.get_indexers().items():
            for ind in indexers:
                cur_indexer = str(ind).split('.')[-1]
                if verbose:
                    print('[INDEXER]:', cur_indexer, 'model', m)
                if indexer == cur_indexer:
                    print('[USING]:', cur_indexer)
                    index_indexer = ind
    # ------------------------------------------
    # Если нужно поработать с конкретной моделью
    # ------------------------------------------
    if '.' in model:
        im_array = model.split(".")
        if not len(im_array) == 2:
            print('[ERROR]: more than 2 parts')
        else:
            app_label, model_name = im_array[0], im_array[1]
            index_model =  apps.get_model(app_label, model_name)
            if not index_model:
                print('[ERROR]: model not found')
            else:
                print('[USING]:', index_model)
    return index_indexer, index_model
    # ----------
    # Индексация
    # ----------
    if options.get("index"):
        query = Change.objects.all()
        total_records = query.aggregate(Count("id"))['id__count']
        print(total_records, 'total_records')
        records_delete = query.filter(action="delete")
        total_records_delete = records_delete.aggregate(Count("id"))['id__count']
        print(total_records_delete, 'total_records_delete')

        # -----------------------------
        # Удаляем записи на удаление
        # их будем делать rebuild_index
        # -----------------------------
        records_delete.delete()
        ferrors.write(now.strftime("%d-%m-%Y %H:%M:%S")+" INDEXING...\n")
        index_opts = {"verbose":True, "verbosity":2, "traceback":True}
        call_command("index", **index_opts)


class Command(BaseCommand):
    """Работа с индексированием, например,
       python manage.py index --rebuild_index
                              --model=flatcontent.Blocks
                              --indexer=blocksindexer
                              --verbose
                              --rebuild_index
    """
    def add_arguments(self, parser):
        parser.add_argument('--verbose',
            action = 'store_true',
            dest = 'verbose',
            default = False,
            help = 'Verbosity output')
        parser.add_argument('--loop',
            action = 'store_true',
            dest = 'loop',
            default = False,
            help = 'Run update loop indefinetely')
        parser.add_argument('--time-out',
            action = 'store',
            dest = 'timeout',
            type = int,
            default = 1,
            help = 'Time to sleep between each query to the database (default: %default)')
        parser.add_argument('--rebuild_index',
            action = 'store_true',
            dest = 'rebuild_index',
            default = False,
            help = 'Rebuild index database')
        parser.add_argument('--per_page',
            action = 'store',
            dest = 'per_page',
            type = int,
            default = 10000,
            help = 'Working page size')
        parser.add_argument('--commit_each',
            action = 'store_true',
            dest = 'commit_each',
            default = False,
            help = 'Commit/flush changes on every document update')
        parser.add_argument('--indexer',
            action = 'store',
            dest = 'indexer',
            type = str,
            default = '',
            help = 'Set indexer')
        parser.add_argument('--model',
            action = 'store',
            dest = 'model',
            type = str,
            default = '',
            help = 'Set model, dot separated')

    help = 'This is the Djapian daemon used to update the index based on djapian_change table'
    requires_model_validation = True

    def handle(self, *app_labels, **options):
        started = time.time()
        utils.load_indexes() # Обязательно для подгрузки индексов
        verbose = options['verbose']
        if not verbose:
            print('use verbose for more information, for example: python manage.py index --verbose --rebuild_index --model=products.ProductsProperties --indexer=productspropertiesindexer')
        loop = options['loop']
        timeout = options['timeout']
        per_page = options['per_page']
        commit_each = options['commit_each']

        indexer, model = get_indexer_and_model(indexer=options['indexer'], model=options['model'], verbose=verbose)
        if options.get('rebuild_index'):
            rebuild_index(indexer=indexer, model=model, verbose=verbose)

            finished = time.time()
            elapsed = finished - started
            print('[ELAPSED]: %.2f sec (%s min)' % (elapsed, int(elapsed/60)))
            return

        if app_labels:
            try:
                app_list = [models.get_app(app_label) for app_label in app_labels]
            except (ImproperlyConfigured, ImportError) as e:
                raise CommandError("%s. Are you sure your INSTALLED_APPS setting is correct?" % e)
            for app in app_list:
                app_models = models.get_models(app, include_auto_created=True)
                if options.get('rebuild_index'): # DEPRECATED
                    rebuild(verbose, per_page, commit_each, app_models)
                else:
                    update_changes(verbose, timeout,
                                   per_page, commit_each, app_models)
        else:
            if options.get('rebuild_index'): # DEPRECATED
                rebuild(verbose, per_page, commit_each)
            else:
                update_changes(verbose, timeout,
                               per_page, commit_each)

