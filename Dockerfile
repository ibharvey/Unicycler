FROM continuumio/miniconda3

RUN apt-get update && \
	apt-get install -y build-essential git cmake autoconf libtool pkg-config zlib1g-dev libbz2-dev

# Easy way to install all required dependencies
RUN conda install -c bioconda unicycler

# Switch with the forked Unicycler package
RUN git clone https://github.com/ibharvey/Unicycler

WORKDIR Unicycler

RUN python3 setup.py install

