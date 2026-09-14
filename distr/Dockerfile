FROM python:3.12
# Пример сборки биндингов
# docker build --progress=plain -t python312 .

ENV BUILD_VERSION=1
ENV LANG C.UTF-8
ENV COLUMNS 5000
ENV TERM xterm-color

RUN apt-get -y update && \
    apt-get -y install apt-utils dialog net-tools git bash mc sudo \
                       ca-certificates libgdal-dev gdal-bin libxapian30 \
                       libxapian-dev python3-xapian \
                       hunspell libhunspell-dev python3-hunspell && \
    pip install sphinx==7.2.6

COPY distr /app/distr

# XAPIAN-BINDINGS
RUN cd /app/distr/ && \
    tar -xf xapian-core-1.4.29.tar.xz && \
    cd /app/distr/xapian-core-1.4.29 && \
    ./configure && make install

RUN cd /app/distr && \
    tar -xf xapian-bindings-1.4.29.tar.xz && \
    cd /app/distr/xapian-bindings-1.4.29 && \
    ./configure --with-python3 && make install

RUN pip install hunspell==0.5.5

