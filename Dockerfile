FROM debian:latest

RUN apt-get update && \
	apt-get install -y build-essential git cmake autoconf libtool pkg-config zlib1g-dev libbz2-dev \
        pilon racon bowtie2 ncbi-blast+ samtools gzip spades bcftools

# Switch with the forked Unicycler package

RUN git clone https://github.com/ibharvey/Unicycler

WORKDIR Unicycler

RUN python3 setup.py install


