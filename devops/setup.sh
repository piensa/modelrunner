#!/bin/bash

if ! conda &> /dev/null
then
    mkdir -p build; cd build
    echo "Download and install miniconda for python 2.7"
    if [[ ! -e Miniconda-3.6.0-Linux-x86_64.sh ]]
    then
        curl -s -o Miniconda-3.6.0-Linux-x86_64.sh http://repo.continuum.io/miniconda/Miniconda-3.6.0-Linux-x86_64.sh
        chmod 755 Miniconda-3.6.0-Linux-x86_64.sh
    fi
    ./Miniconda-3.6.0-Linux-x86_64.sh -b -p $HOME/miniconda
    cd
    grep '/home/mr/miniconda/bin:' .bashrc || echo 'export PATH="/home/mr/miniconda/bin:$PATH"' >> .bashrc
fi

# bashrc may not have been sourced if run from remote
export PATH="/home/mr/miniconda/bin:$PATH"

conda config --set always_yes yes --set changeps1 no
conda update -q conda

# need conda build
conda install conda-build