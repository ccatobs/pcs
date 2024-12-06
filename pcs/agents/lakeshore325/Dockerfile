# bftc-docker
# A container setup with an installation of the bftc agent

# Use the ocs image as a base
FROM simonsobs/ocs:v0.10.4

# Copy in and install requirements
COPY requirements.txt /app/pcs/requirements.txt
WORKDIR /app/pcs/
RUN pip3 install -r requirements.txt

# Copy the current directory contents into the container at /app
COPY ../../../ /app/pcs/

# Reset workdir to avoid local imports
WORKDIR /

# Run agent on container startup
ENTRYPOINT ["dumb-init", "ocs-agent-cli"]
