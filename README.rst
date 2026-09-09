Описание
-----------
Менеджер для работы с индексным поиском

-----------
Xapian and Hunspell
Mac OS:
brew install --build-from-source xapian
brew install --build-from-source hunspell
uv pip install --no-cache-dir xapian-bindings
python -c "import xapian; print(xapian.__version__)"
uv pip install --no-cache-dir hunspell

На MacOs при ошибках
  ld: library 'hunspell' not found
  'hunspell.hxx' file not found
в ошибке
-I/usr/local/Cellar/hunspell/1.6.2/include/hunspell
смотрит на старую версию, можно подложить туда символическую ссылку на новую
+ обязательно
ln -s /opt/homebrew/Cellar/hunspell/1.7.3/lib/libhunspell-1.7.dylib /opt/homebrew/Cellar/hunspell/1.7.3/lib/libhunspell.dylib

export CFLAGS=$(pkg-config --cflags hunspell) && \
export LDFLAGS=$(pkg-config --libs hunspell) && \
export CPPFLAGS=$(pkg-config --cflags hunspell) && \
uv pip install hunspell --no-cache

Для Ubuntu с докером:
лучше собирать биндинги вручную, а не через xapian-bindings-0.1 (т/к недоступен постоянно oligarchy.co.uk)
RUN cd /app/distr/ && \
    tar -xf xapian-core-1.4.29.tar.xz && \
    cd /app/distr/xapian-core-1.4.29 && \
    ./configure && make install

RUN cd /app/distr && \
    tar -xf xapian-bindings-1.4.29.tar.xz && \
    cd /app/distr/xapian-bindings-1.4.29 && \
    ./configure --with-python3 && make install

Словари hunspell
Файл .dic (Dictionary):
  Содержит общее число слов на первой строке.
  Хранит список базовых слов (лемм/основ).
  Содержит буквенные или цифровые флаги после слеша (например, слово/A), которые указывают, какие правила из .aff применимы к этому слову

Файл .aff (Affix):
  Задает правила добавления приставок (префиксов) и суффиксов (постфиксов).
  Объявляет кодировку текста (например, SET UTF-8).
  Настраивает правила замены символов, флаги для сложных слов и алгоритмы генерации форм

Оба файла должны иметь одинаковое имя (например, ru_RU.dic и ru_RU.aff) и находиться в одной директории

Установка пакетом
-----------
Для локальной разработки::
    pip install -e packages/djapian
    uv pip install -e packages/djapian

В settins.py::
    DJAPIAN_STEMMING_LANG = 'ru'
    DJAPIAN_DATABASE_PATH = os.path.join(MEDIA_ROOT, 'djapian_base') # путь к индексной базе
    DJAPIAN_VOCA = os.path.join(BASE_DIR, 'xapian64', 'spell') # путь к словарям
    INSTALLED_APPS = [... 'djapian', ...]

В urls.py::
    if 'djapian' in settings.INSTALLED_APPS:
        from djapian.utils import load_indexes
        load_indexes()

Для апи, в conf/main.py::
    from django.conf import settings
    if 'djapian' in settings.INSTALLED_APPS:
        from djapian.utils import load_indexes
        load_indexes()

Использование
-----------
Проверка::
    import djapian

Рядом с models.py положить файл для индексирования модели::
    from djapian.forindex import space, NewIndexer
    from apps.products.models import Products
    class ProductsIndexer(NewIndexer):
        fields = (
            ('get_properties_search_str', 1),
        )
        tags = [
            ('brand_str', 'get_product_brand_for_xapian', 10),
            ('brand', 'get_product_brand'),
            ('id', 'id'),
            ('price__range', 'price_for_xapian'), # для диапазонов + для сортировки min/max
    ]
    space.add_index(Products, ProductsIndexer, attach_as='indexer')

Использование::
    q = 'ботинки'
    query_priority = ' AND brand:1'
    result = Products.indexer.search(q + query_priority, order_by=['price__range', False])

Для полной переиндексации::
    from django.core import management
    management.call_command('index', '--rebuild')

Удаление
-----------
Удалить пакет::
    pip uninstall djapian

Для создания пакета
https://docs.python.org/3.10/distutils/introduction.html#distutils-simple-example
https://docs.python.org/3.10/distutils/sourcedist.html
::
    python setup.py sdist




