import datetime
from decimal import Decimal
from collections.abc import Iterable
from functools import reduce
import os

import xapian

from django.db import models
from djapian.signals import post_save, pre_delete
from django.conf import settings
from django.utils.encoding import smart_str as smart_text, force_str as force_text
from djapian import decider

from djapian.database import CompositeDatabase
from djapian.resultset import ResultSet
from djapian.utils.paging import paginate
from djapian.utils.commiter import Commiter

from djapian.utils.decorators import reopen_if_modified
from djapian.utils import DEFAULT_WEIGHT, model_name


def is_iterable(x):
    return isinstance(x, Iterable)


class Field(object):
    raw_types = (
        int,
        float,
        str,
        bool,
        models.Model,
        datetime.time,
        datetime.date,
        datetime.datetime,
        #Decimal, # можно альтернативно использовать для xapian.sortable_serialise
        bytes, # для xapian.sortable_serialise(float(self.field_name))
)

    def __init__(self, path, model, weight=DEFAULT_WEIGHT, prefix='', number=None):
        self.path = path
        self.weight = weight
        self.prefix = prefix
        self.number = number
        self.model = model

    def get_tag(self):
        return self.prefix.upper()

    #def convert(self, field_value, model):
    def convert(self, field_value):
        """
        Generates index values (for sorting) for given field value and its content type
        Например,
        {'product_id': b'000000000362', 'price__range': b'\xca\x04'} price__range должен быть в таком же формате, что и в запросе => Query(VALUE_RANGE 14 \xa6 \x80), где 14 - индекс поля, остальное диапазон
        """
        if field_value is None:
            return None
        # If it is a model field make some postprocessing of its value
        #try:
            #content_type = model._meta.get_field(self.path.split('.', 1)[0])
        #except models.FieldDoesNotExist:
            #content_type = field_value

        # ПО контентному типу работает только если мы передаем из модели
        # а если мы передаем результат функции, например, из тега,
        # надо ориентироваться на тип значения
        #content_type = self._get_content_type(field_value)

        value = field_value
        if isinstance(field_value, (models.IntegerField, int)):
            # Integer fields are stored with 12 leading zeros
            value = '%012d' % field_value
        elif isinstance(field_value, (models.BooleanField, bool)):
            # Boolean fields are stored as 't' or 'f'
            if field_value:
                value = 't'
            else:
                value = 'f'
        elif isinstance(field_value, (models.DateTimeField, datetime.datetime)):
            # DateTime fields are stored as %Y%m%d%H%M%S (better sorting)
            value = field_value.strftime('%Y%m%d%H%M%S')
        elif isinstance(field_value, (float, models.FloatField)):
            value = '%.10f' % value
        elif isinstance(field_value, (bytes, )):
            #value = xapian.sortable_serialise(float(value))
            pass

        return value

    def resolve_one(self, value, name):
        value = getattr(value, name)

        if isinstance(value, models.Manager):
            value = value.all()
        elif callable(value):
            value = value()

        return value

    def resolve(self, value):
        bits = self.path.split('.')

        for bit in bits:
            if is_iterable(value):
                value = u', '.join(
                    map(lambda v: force_text(self.resolve_one(v, bit)), value)
                )
            else:
                value = self.resolve_one(value, bit)

        if isinstance(value, self.raw_types):
            return value
        if is_iterable(value):
            return u', '.join(map(force_text, value))
        return value and force_text(value) or None

    def extract(self, document):
        if self.number:
            return document.get_value(self.number)
        return None

    def _get_content_type(self, field_value):
        """Returns field's models.Field instance or value if such model field does not exist"""
        try:
            return self.model._meta.get_field(self.path.split('.', 1)[0])
        except models.FieldDoesNotExist:
            return field_value


class Indexer(object):
    field_class = Field
    decider = decider.CompositeDecider
    free_values_start_number = 11

    fields = []
    tags = []
    aliases = {}
    trigger = lambda indexer, obj: True
    stemming_lang_accessor = None
    stemmer_class = xapian.Stem
    stopper = None
    # Фасетные свойства, указываем только имена
    # по именам найдем индексы полей и добавим в
    # enquire.add_matchspy(xapian.ValueCountMatchSpy(index))

    #flags = type.__new__(
        #type,
        #'SearchFlags',
        #(object,),
        #dict([
            #(name[5:], value)\
                #for name, value in xapian.QueryParser.__dict__.iteritems()\
                    #if name.startswith('FLAG_')
        #])
    #)

    def __init__(self, db, model):
        """
        Initialize an Indexer whose index data to `db`.
        `model` is the Model whose instances will be used as documents.
        Note that fields from other models can still be used in the index,
        but this model will be the one returned from search results.
        """
        self._prepare(db, model)

        # Parse fields
        # For each field checks if it is a tuple or a list and add it's weight
        for field in self.__class__.fields:
            if isinstance(field, (tuple, list)):
                self.fields.append(self.field_class(field[0], self._model, field[1]))
            else:
                self.fields.append(self.field_class(field, self._model))

        # Parse prefixed fields
        valueno = self.free_values_start_number

        for field in self.__class__.tags:
            tag, path = field[:2]
            if len(field) == 3:
                weight = field[2]
            else:
                weight = DEFAULT_WEIGHT

            self.tags.append(self.field_class(path, self._model, weight, prefix=tag, number=valueno))
            valueno += 1

        for tag, aliases in self.__class__.aliases.items():
            if self.has_tag(tag):
                if not isinstance(aliases, (list, tuple)):
                    aliases = (aliases,)
                self.aliases[tag] = aliases
            else:
                raise ValueError("Cannot create alias for tag `%s` that doesn't exist" % tag)

        models.signals.post_save.connect(post_save, sender=self._model)
        models.signals.pre_delete.connect(pre_delete, sender=self._model)


    def __unicode__(self):
        return self.__class__.get_descriptor()
    __str__ = __unicode__

    def has_tag(self, name):
        return self.tag_index(name) is not None

    def tag_index(self, name):
        for field in self.tags:
            if field.prefix == name:
                return field.number

        return None

    def get_stemmer(self, stemming_lang):
        """
        Return a stemmer instance for the requested stemming language.
        """
        # Just returns a stemmer instance. We do not provide any optimization like
        # instance memoization here because the default stemmer is stateless.
        return self.stemmer_class(stemming_lang)

    def get_stopper(self, lang):
        """
        Return a stopper instance for the requested language.
        """
        # There are no default stop words lists bundled with Xapian.
        return self.stopper

    @classmethod
    def get_descriptor(cls):
        return ".".join([cls.__module__, cls.__name__]).lower()

    # Public Indexer interface

    def update(self, documents=None, after_index=None, per_page=10000, commit_each=False):
        """
        Update the database with the documents.
        There are some default value and terms in a document:
         * Values:
           1. Used to store the ID of the document
           2. Store the model of the object (in the string format, like
              "project.app.model")
           3. Store the indexer descriptor (module path)
           4..10. Free

         * Terms
           UID: Used to store the ID of the document, so we can replace
                the document by the ID
        """
        # Open Xapian Database
        database = self._db.open(write=True)

        # If doesnt have any document at all
        if documents is None:
            update_queue = self._model.objects.all()
        else:
            update_queue = documents

        commiter = Commiter.create(commit_each)(
            #lambda: database.begin_transaction(flush=True),
            database.begin_transaction,
            database.commit_transaction,
            database.cancel_transaction
        )

        # Get each document received
        for page in paginate(update_queue, per_page):
            print('update progress: %s' % page)
            try:
                commiter.begin_page()

                #counter = 0
                for obj in page.object_list:

                    #counter += 1
                    #if counter % 50 == 0:
                    #    print(counter, len(page.object_list))

                    commiter.begin_object()
                    try:
                        if not self.trigger(obj):
                            self.delete(obj.pk, database)
                            continue

                        doc = xapian.Document()

                        # Add default terms and values
                        uid = self._create_uid(obj)
                        doc.add_term(self._create_uid(obj))
                        self._insert_meta_values(doc, obj)

                        generator = xapian.TermGenerator()
                        generator.set_database(database)
                        generator.set_document(doc)
                        generator.set_flags(xapian.TermGenerator.FLAG_SPELLING)

                        #stem_lang = self._get_stem_language(obj)
                        #if stem_lang:
                            #generator.set_stemmer(xapian.Stem(stem_lang))
                            #stopper = self.get_stopper(stem_lang)
                            #if stopper:
                                #generator.set_stopper(stopper)
                        stemming_lang = self._get_stem_language(obj)
                        if stemming_lang:
                            stemmer = self.get_stemmer(stemming_lang)
                            generator.set_stemmer(stemmer)
                            stopper = self.get_stopper(stemming_lang)
                            if stopper:
                                generator.set_stopper(stopper)

                        #for field in self.fields + self.tags:
                            # Trying to resolve field value or skip it
                            #try:
                                #value = field.resolve(obj)
                            #except AttributeError:
                                #continue
                            #if field.prefix:
                                #index_value = field.convert(value, self._model)
                                #if index_value is not None:
                                    #doc.add_value(field.number, smart_text(index_value))
                            #prefix = smart_text(field.get_tag())
                            #generator.index_text(smart_text(value), field.weight, prefix)
                            #if prefix:  # if prefixed then also index without prefix
                                #generator.index_text(smart_text(value), field.weight)
                        #database.replace_document(uid, doc)
                        #if after_index:
                            #after_index(obj)

                        # Get a weight for the object
                        obj_weight = self._get_object_weight(obj)
                        # Index fields
                        self._do_index_fields(doc, generator, obj, obj_weight)

                        database.replace_document(uid, doc)
                        if after_index:
                            after_index(obj)

                        commiter.commit_object()
                    except Exception:
                        commiter.cancel_object()
                        raise

                commiter.commit_page()
            except Exception:
                commiter.cancel_page()
                raise

        database.commit()

    def search(self, query, flags = None, facets: list = None, order_by: list = None):
        """Поиск по индексу, например:
           doc_count = ProductsProperties.indexer.document_count()
           search_result = ProductsProperties.indexer.search(
              xapian.Query.MatchAll,
           )
           search_result = ProductsProperties.indexer.search(
              'get_product_id:222085',
           )
           :param query: запрос строкой
           :param flags: флаги поиска, смотреть в ResultSet # https://xapian.org/docs/queryparser.html
                         FLAG_BOOLEAN: Enables support for boolean operators like AND, OR, NOT, and bracketed expressions.
                         FLAG_PHRASE: Enables support for quoted phrase expressions ("")).
                         FLAG_LOVEHATE: Enables support for + (mandatory) and - (prohibited) operators.
                         FLAG_BOOLEAN_ANY_CASE: Enables support for lowercase or mixed-case boolean operators (e.g., and, or).
                         FLAG_WILDCARD: Enables support for wildcard matching (such as *).
                         FLAG_PARTIAL: Treats the final word of an interactive search as a wildcard match automatically unless followed by whitespace.
                         FLAG_NO_PROPER_NOUN_HEURISTIC: Disables the special capitalized proper-noun handling (added in Xapian 2.0)
           :param facets: фасеты списком по которым надо считать количество вхождений
           :param order_by: сортировка ['RELEVANCE', False] / [None, False] / ['-price', False]
                            relevance_first - второй элемент массива, первый - поле для сортировки
        """
        if isinstance(flags, str):
            if flags == 'bool':
                flags = xapian.QueryParser.FLAG_BOOLEAN
            elif flags == 'partial':
                flags = xapian.QueryParser.FLAG_PARTIAL
            else:
                flags = None
        else:
            flags = xapian.QueryParser.FLAG_PARTIAL | xapian.QueryParser.FLAG_BOOLEAN
        return ResultSet(self, query, flags=flags, facets=facets, order_by=order_by)

    def delete(self, obj, database=None):
        """
        Delete a document from index
        """
        try:
            if database is None:
                database = self._db.open(write=True)
            database.delete_document(self._create_uid(obj))
        except (IOError, RuntimeError, xapian.DocNotFoundError) as e:
            pass

    def document_count(self):
        return self._db.document_count()

    __len__ = document_count

    def clear(self):
        self._db.clear()

    # Private Indexer interface
    def _prepare(self, db, model=None):
        """Initialize attributes"""
        self._db = db
        self._model = model
        self._model_name = model and model_name(model)

        self.fields = [] # Simple text fields
        self.tags = [] # Prefixed fields
        self.aliases = {}

    def _get_meta_values(self, obj):
        if isinstance(obj, models.Model):
            pk = obj.pk
        else:
            pk = obj
        return [pk, self._model_name, self.__class__.get_descriptor()]

    def _insert_meta_values(self, doc, obj, start=1):
        for value in self._get_meta_values(obj):
            doc.add_value(start, smart_text(value))
            start += 1
        return start

    def _create_uid(self, obj):
        """
        Generates document UID for given object
        """
        return "UID-" + "-".join(map(smart_text, self._get_meta_values(obj)))

    def _do_search(self,
                   query,
                   offset,
                   limit,
                   order_by,
                   flags,
                   stemming_lang,
                   filter,
                   exclude,
                   collapse_by,
                   stopper,
                   facets: list = None):
        """
        flags are as defined in the Xapian API :
        http://www.xapian.org/docs/apidoc/html/classXapian_1_1QueryParser.html
        Combine multiple values with bitwise-or (|)

        ФАСЕТЫ: https://getting-started-with-xapian.readthedocs.io/en/latest/howtos/facets.html
        передаем список номеров для enquire.add_matchspy(xapian.ValueCountMatchSpy(1)), где 1 - номер поля в индексе
        Шпионы для фасетов по количеству (нужен номер поля)
        #    for facet_name, facet_index in self.facets.items():
        #        enquire.add_matchspy(spy_index)
        :param facets: список имен тегов для агрегации в spy
        """
        if not facets:
            facets = []
        database = self._db.open()
        enquire = xapian.Enquire(database)

        if order_by is None or order_by[0] in (None, 'RELEVANCE'):
            enquire.set_sort_by_relevance()
        else:
            sort_reversed = False
            order_by, relevance_first = order_by
            if order_by.startswith('-'):
                sort_reversed = True

            if order_by[0] in '+-':
                order_by = order_by[1:]

            try:
                valueno = self.tag_index(order_by)
            except (ValueError, TypeError):
                raise ValueError("Field %s cannot be used in order_by clause"
                                 " because it doen't exist in index" % order_by)

            if relevance_first:
                enquire.set_sort_by_relevance_then_value(valueno, sort_reversed)
            else:
                enquire.set_sort_by_value_then_relevance(valueno, sort_reversed)

        if collapse_by:
            try:
                valueno = self.tag_index(collapse_by)
            except (ValueError, TypeError):
                raise ValueError("Field %s cannot be used in set_collapse_key"
                                 " because it doen't exist in index" % collapse_by)
            enquire.set_collapse_key(valueno)

        query, query_parser = self._parse_query(query, database, flags, stemming_lang, stopper)
        enquire.set_query(query)

        decider = self.decider(self._model, self.tags, filter, exclude)

        if limit is None:
            limit = self.document_count()

        spies = {}
        for facet_name in facets:
            if self.has_tag(facet_name):
                ind = self.tag_index(facet_name)
                spy = xapian.ValueCountMatchSpy(ind)
                enquire.add_matchspy(spy)
                spies[facet_name] = spy

        result = reopen_if_modified(database)(
            lambda: enquire.get_mset(offset, limit, None, decider)
        )(), query, query_parser, spies

        # Пример вывода агрегации по количеству найденных фасетов
        #for facet_name, spy in spies.items():
        #    for facet in spy.values():
        #        print('%(facet_name)s: %(term)s; count: %(count)i' % {
        #            'facet_name': facet_name,
        #            'term' : facet.term,
        #            'count' : facet.termfreq
        #        })

        return result

    def _get_stem_language(self, obj=None):
        """
        Returns stemmig language for given object if acceptable or model wise
        """
        # Use the language defined in DJAPIAN_STEMMING_LANG
        language = getattr(settings, 'DJAPIAN_STEMMING_LANG', 'none')
        if language == 'multi':
            language = 'none'

            if obj:
                try:
                    language = self.field_class(
                        self.stemming_lang_accessor, self._model
                    ).resolve(obj)
                except AttributeError:
                    pass
        return language

    def _get_query_parser(self, stemming_lang, stopper=None, range_processor_suffix: str = '__range'):
        """
        Creates a Xapian QueryParser object and applies
        a stemmer, a stopper and prefixes for tags and aliases
        """
        query_parser = xapian.QueryParser()
        query_parser.set_default_op(xapian.Query.OP_AND)

        for field in self.tags:
            field_prefix = field.prefix.lower()
            query_parser.add_prefix(field_prefix, field.get_tag())

            if field_prefix.endswith(range_processor_suffix):
                range_processor = xapian.NumberRangeProcessor(field.number, '%s:' % field_prefix)
                query_parser.add_rangeprocessor(range_processor)
            if field.prefix in self.aliases:
                for alias in self.aliases[field.prefix]:
                    query_parser.add_prefix(alias, field.get_tag())
        if stemming_lang in (None, 'none'):
            stemming_lang = self._get_stem_language()

        if stemming_lang:
            stemmer = self.get_stemmer(stemming_lang)
            query_parser.set_stemmer(stemmer)
            query_parser.set_stemming_strategy(xapian.QueryParser.STEM_SOME)

            if not stopper:
                stopper = self.get_stopper(stemming_lang)

        if stopper:
            query_parser.set_stopper(stopper)

        return query_parser

    def _parse_query(self, term, db, flags, stemming_lang, stopper=None):
        """
        Parses search queries

        Пример поиска по диапазону
        Xapian::Query queryLeft(Xapian::Query::OP_VALUE_RANGE, 0,
                Xapian::sortable_serialise(1), Xapian::sortable_serialise(2));
        Xapian::Query queryRight(Xapian::Query::OP_VALUE_RANGE, 0,
                Xapian::sortable_serialise(8), Xapian::sortable_serialise(11));
        Xapian::Query query(Xapian::Query::OP_OR, queryLeft, queryRight);

        cout << "Parsed query is: " << query.get_description() << endl;
        enquire.set_query(query);
        """
        # Instance Xapian Query Parser
        #query_parser = xapian.QueryParser()
        #for field in self.tags:
            #query_parser.add_prefix(field.prefix.lower(), field.get_tag())
            #if field.prefix in self.aliases:
                #for alias in self.aliases[field.prefix]:
                    #query_parser.add_prefix(alias, field.get_tag())
        #query_parser.set_database(db)
        #query_parser.set_default_op(xapian.Query.OP_AND)
        #if stemming_lang in (None, "none"):
            #stemming_lang = self._get_stem_language()
        #if stemming_lang:
            #query_parser.set_stemmer(xapian.Stem(stemming_lang))
            #query_parser.set_stemming_strategy(xapian.QueryParser.STEM_SOME)
            #if not stopper:
                #stopper = self.get_stopper(stemming_lang)
        #if stopper:
            #query_parser.set_stopper(stopper)

        query_parser = self._get_query_parser(stemming_lang, stopper)
        query_parser.set_database(db)

        # Можно передавать xapian.QueryMatchAll,
        # чтобы получить все документы
        if isinstance(term, xapian.Query) or term == xapian.Query.MatchAll:
            return term, query_parser

        # нужно найти индексы и добавить в них range processors
        #money = xapian.NumberRangeProcessor(0, 'price')
        #query_parser.add_rangeprocessor(money) # $300..800

        parsed_query = query_parser.parse_query(term, flags)
        return parsed_query, query_parser

    def _get_object_weight(self, obj):
        """
        Returns a default weight value for the object. 
        """
        if hasattr(self.__class__, 'weight'):
            obj_weight = self.field_class(self.__class__.weight, self._model).resolve(obj)
        else:
            obj_weight = DEFAULT_WEIGHT
        return obj_weight

    def _do_index_fields(self, doc, generator, obj, obj_weight):
        """
        Indexes fields of the object.
        """
        for field in self.fields + self.tags:
            # Trying to resolve field value or skip it
            try:
                value = field.resolve(obj)
            except AttributeError:
                continue

            if value is None:
                continue

            if field.prefix:
                doc.add_value(field.number, field.convert(value))

            prefix = smart_text(field.get_tag())
            value = smart_text(value)

            generator.index_text(value, field.weight*obj_weight, prefix)
            if prefix:  # if prefixed then also index without prefix
                generator.index_text(value, field.weight*obj_weight)

class CompositeIndexer(Indexer):
    def __init__(self, *indexers):
        self._indexers = indexers
        self._prepare(
            db=CompositeDatabase([indexer._db for indexer in indexers])
        )

    def clear(self):
        raise NotImplementedError

    def update(self, *args):
        raise NotImplementedError

    def tag_index(self, name):
        return reduce(lambda a, b: a == b and a or None,
                      [indexer.tag_index(name) for indexer in self._indexers])
