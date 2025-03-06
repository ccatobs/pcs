# pcs-docker
# A container setup with an installation of pcs.

# Use the SO ocs image as a base
# (As of Mar 2025) Uses Ubuntu 22.04, installs Python, makes venv
FROM simonsobs/ocs:v0.11.3-19-gd729e04

# Copy in and install requirements
COPY requirements.txt /app/pcs/requirements.txt
WORKDIR /app/pcs/
# Work around https://github.com/pypa/setuptools/issues/4483/ temporarily
RUN python -m pip install -U "setuptools<71.0.0"
RUN python -m pip install -r requirements.txt
RUN python -m pip uninstall -y opencv-python && \
    python -m pip install opencv-python-headless

# Copy the current directory contents into the container at /app
COPY . /app/pcs/

# Install pcs
RUN python -m pip install .

# Reset workdir to avoid local imports
WORKDIR /

# Run agent on container startup
ENTRYPOINT ["dumb-init", "ocs-agent-cli"]
