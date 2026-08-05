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
uv pip install --no-cache-dir hunspell2
python -c "from hunspell2 import HunSpell; #см forindex.py"

Установка пакетом
-----------
Для локальной разработки::
    pip install -e packages/djapian
    uv pip install -e packages/djapian

Импорт
-----------
Проверка::
    import djapian


Удаление
-----------
Удалить пакет::
    pip uninstall djapian

Для создания пакета
https://docs.python.org/3.10/distutils/introduction.html#distutils-simple-example
https://docs.python.org/3.10/distutils/sourcedist.html
::
    python setup.py sdist




