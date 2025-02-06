FROM simonsobs/ocs:v0.10.4

# Set working directory
WORKDIR /app/bfcu/

# Copy in local project files
COPY . .

# Install additional Python dependencies
RUN pip3 install -r requirements.txt
RUN git clone -b bftc_updates http://github.com/ccatobs/pcs && \
    cd /app/bftc/pcs && \
    pip install . && \
    cd .. && \
    rm -r pcs
    
# Run agent on container startup
ENTRYPOINT ["dumb-init", "ocs-agent-cli","--agent", "agent.py","--entrypoint","main","--instance-id","bluefors-cu"]

# Replace ENTRYPOINT to avoid running agent.py (good debugging tool)
#ENTRYPOINT ["tail", "-f", "/dev/null"]
