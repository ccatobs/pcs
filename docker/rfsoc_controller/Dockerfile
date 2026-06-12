FROM pcs

# Will eventually want to pip install ccatkidlib from github once made public
WORKDIR /app/pcs/
RUN git clone https://github.com/ccatobs/ccatkidlib.git 

RUN python -m pip install -e ./ccatkidlib && \
    python -m pip install -r ./ccatkidlib/requirements.txt && \
    python -m pip install numpy==2.0.2

WORKDIR /

ENTRYPOINT ["dumb-init", "ocs-agent-cli"]
