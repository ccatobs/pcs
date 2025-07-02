FROM pcs

# Will eventually want to pip install ccatkidlib from github once made public
WORKDIR /app/pcs/
COPY ./ccatkidlib /app/pcs/ccatkidlib 

RUN python -m pip install -e ./ccatkidlib && \
    python -m pip install -r ./ccatkidlib/requirements.txt && \
    python -m pip install numpy==2.0.2

WORKDIR /

ENV OCS_CONFIG_DIR='/config'

ENTRYPOINT ["dumb-init", "ocs-agent-cli"]