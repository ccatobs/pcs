FROM simonsobs/ocs:v0.10.4

# Set working directory
WORKDIR /app/lakeshore325/

# Copy in local project files
COPY . .

# Install additional Python dependencies
RUN pip3 install -r requirements.txt
RUN git clone http://github.com/ccatobs/pcs && \
    cd /app/lakeshore325/pcs && \
    pip install . && \
    cd .. && \
    rm -r pcs
    
# Run agent on container startup
ENTRYPOINT ["dumb-init", "ocs-agent-cli","--agent", "agent.py","--entrypoint","main","--instance-id","LSA2PP1"]

# Replace ENTRYPOINT to avoid running agent.py (good debugging tool)
#ENTRYPOINT ["tail", "-f", "/dev/null"]
