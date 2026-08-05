# -*- coding: utf-8 -*-
import os
import re

import xapian
import hunspell
import hashlib

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.utils.encoding import smart_str as smart_text

from djapian import space
from djapian.indexer import Field, Indexer, CompositeIndexer
from djapian.models import Change as DjapianChanges
from djapian.space import IndexSpace
from djapian.utils import load_indexes
from djapian.resultset import ResultSet

space = IndexSpace(settings.DJAPIAN_DATABASE_PATH, 'global')
add_index = space.add_index
rega_strict_text = re.compile('[^0-9a-zA-Zа-яА-ЯёЁ/-]', re.U+re.I+re.DOTALL)

def kill_quotes(item, rega=None, replace: str = ''):
    """Замена в строке на replace символы
       (обычно на "", но можно на пробел
       Своя регулярка, например,
       заменить несколько пробелов на один
       kill_quotes(item=' Радужный  42', rega=re.compile('rega[\s]+', re.I+re.U+re.DOTALL), replace=' ')
    """
    if not rega:
        rega = rega_strict_text
    return rega.sub(replace, item)

def get_indexes():
    """Вытащить все индексы app:model"""
    result = {}
    indexers = space.get_indexers()
    for i in indexers.keys():
        label = i._meta.app_label
        name = i.__name__
        if not label in result:
            result[label] = []
        result[label].append(name)
    return result

def get_hunspell_words(search_terms):
    """Hunspell suggest
       вспомогательная функция для проверки словаря
       :param search_terms: поисковая фраза
       :return: arr bytes, нужно декодировать
       .decode('utf-8') каждый элемент
    """
    return HUNSPELL_VOCABULARY.suggest(search_terms)

def get_xapian_words(result):
    """Xapian suggest
       Вспомогательная функция для проверки стандартного стеммера
       :param result: RSet
    """
    corrected_query = result.get_corrected_query_string()
    result._query_parser.set_stemming_strategy(xapian.QueryParser.STEM_ALL)
    search_terms = list(result._query_parser.parse_query(result._query_str))
    rega = re.compile('[A-Z]+', re.U)
    result_terms = []
    for term in search_terms:
        term = rega.sub('', term)
        result_terms.append(term)
    return result_terms

def get_xapian_stopper(stopwords: list = None):
    """hunspell_stopper stop list
       Надо вызывать функцию get_xapian_stopper,
       чтобы быть уверенным, что stopwords заполнено"""
    _stopper = xapian.SimpleStopper()
    if stopwords is None:
        base_path = settings.DJAPIAN_VOCA
        stopwords_file = os.path.join(base_path, '%s.stop' % settings.DJAPIAN_STEMMING_LANG)
        # ---------------------------------
        # Проверяем, что такой файл имеется
        # ---------------------------------
        if os.path.exists(stopwords_file):
            with open(stopwords_file) as f:
                stopwords = f.readlines()
    elif isinstance(stopwords, str):
        stopwords = stopwords.split()
    for word in stopwords:
        _stopper.add(word)
    return _stopper

class HunspellStem(xapian.StemImplementation):
    dic = {'залипон': 'залипуха'}
    def __init__(self):
        super(HunspellStem, self).__init__()
        self._stem = XAPIAN_STEMMER
        self._h = HUNSPELL_VOCABULARY

    def __call__(self, s):
        s = s.decode('utf-8').replace('ё', 'е')
        return self._do_stem(s)

    def _do_stem(self, s):
        # ------------------------
        # гоним по myspell словарю
        # ------------------------
        res = self._h.stem(s)
        if len(res):
            # ------------------------
            # Мы нашли слово в словаре
            # ------------------------
            #print('_h.stem(s)', res[0])
            return res[0]
        # ----------------------------
        # гоним по внутреннему словарю
        # ----------------------------
        res = self.dic.get(s, '')
        if res:
            #print('self.dic.get(s, "")', res)
            return res
        # ------------------------------
        # гоним по стандартному стеммеру
        # ------------------------------
        res = self._stem(s)
        #print('_stem(s)', res)
        return res


class NewIndexer(Indexer):
    """Модель индекса"""
    def get_stemmer(self, stemming_lang):
        return HUNSPELL_STEMMER

    def get_stopper(self, lang):
        return get_xapian_stopper()

    def _do_index_fields(self, doc, generator, obj, obj_weight):
        """Do not honor terms position during indexing.
           This should lead to a lesser disk space usage
        """
        for field in self.fields + self.tags:
            # Trying to resolve field value or skip it
            # Отладочка:
            # print(field, field.resolve(obj))
            try:
                value = field.resolve(obj)
                if value is None:
                    continue
            except AttributeError:
                continue
            if field.prefix:
                fvalue = field.convert(value)
                doc.add_value(field.number, fvalue)
            prefix = smart_text(field.get_tag())
            # Уже в байтах, используем для диапазонов (xapian.sortable_serialise(float(self.field_name)))
            if not isinstance(value, bytes):
                value = smart_text(value)
            generator.index_text_without_positions(value, field.weight*obj_weight, prefix)
            if prefix:  # if prefixed then also index without prefix
                generator.index_text_without_positions(value, field.weight*obj_weight)


def whata_terms(RSet, encode=False):
    """GET search terms from ResultSet"""
    search_terms = []
    if not type(RSet) == ResultSet:
        return []
    # ----------------------------------
    # Рассчитываем стандартным стеммером
    # ----------------------------------
    stemming_lang = settings.DJAPIAN_STEMMING_LANG
    stemmer = xapian.Stem(stemming_lang)
    # ----------------------
    # Сначала достаем слова,
    # учитывая стоп-лист
    # А потом будем резать
    # ----------------------
    query_terms = RSet.get_parsed_query_terms(False)
    for word in query_terms:
        term = stemmer(word)
        #print(word, term)
        if encode:
            term = term.decode('utf-8')
        search_terms.append(term)
    return search_terms

def hershin(string, result):
    """1) Заменить в строке запятые на пробелы
       2) Разбить по пробелам строку
       3) Проверить не входит ли уже это слово в результат
       4) Добавить в кортеж в нижнем регистре
    """
    if not string:
        return result
    if isinstance(string, int):
        string = '%s' % string
    string = kill_quotes(item=string, replace=' ')
    string = string.split()
    for item in string:
        if item:
            if item == '-':
                continue
            item = item.lower()
            if not item in result:
                result.append(item)
    return result

def work_for_djapian(model, pks: list, action: str = 'edit'):
    """Работа за djapian т.к. этот даун не знает,
       что надо reindex при queryset.update
       pks - массив айдишников
       :param model: модель
       :param pks: список ид
       :param action: действие с моделью
    """
    if not pks:
        return 0
    app_label = model._meta.app_label.lower()
    model_name = model.__name__.lower()
    content_type = ContentType.objects.get(app_label=app_label, model=model_name)
    # ---------------------------------------------------
    # Уже имеющаяся информация о необходимости индексации
    # ---------------------------------------------------
    exists = DjapianChanges.objects.filter(content_type=content_type, object_id__in=pks).values_list('object_id', flat=True)
    new_pks = [pk for pk in pks if not str(pk) in exists]
    for pk in new_pks:
        DjapianChanges.objects.create_raw(
            content_type=content_type,
            object_id=pk,
            action=action, )
    return 1

def serp_hash(words):
    """Хэш текста из слов
       На вход принимаем результат whata_terms
    """
    result = []
    hash_words = []
    if isinstance(words, (list, tuple)):
        for item in words:
            word = hashlib.md5()
            word.update(item)
            hash_words.append(word.hexdigest())
    else:
        word = hashlib.md5()
        word.update(words)
        hash_words.append(word.hexdigest())
    new_words = 0
    # По каждому слову
    for word in hash_words:
        # int("f", 16) = 15
        new_word = ""
        # Каждый символ из 16 в 10 систему для сложения
        for letter in word:
            new_word += str(HEX_STR.index(letter))
        try:
            new_word = int(new_word)
        except ValueError:
            new_word = 0
        new_words += new_word
    return new_words

HEX_STR = ('0', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'a', 'b', 'c', 'd', 'e', 'f')
HUNSPELL_VOCABULARY = hunspell.HunSpell(
    os.path.join(settings.DJAPIAN_VOCA, '%s.dic' % settings.DJAPIAN_STEMMING_LANG),
    os.path.join(settings.DJAPIAN_VOCA, '%s.aff' % settings.DJAPIAN_STEMMING_LANG), )
XAPIAN_STEMMER = xapian.Stem(settings.DJAPIAN_STEMMING_LANG)
HUNSPELL_STEMMER = xapian.Stem(HunspellStem())
