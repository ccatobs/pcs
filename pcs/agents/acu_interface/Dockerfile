# ACU Interace Agent
# PrimeCam Agent for commanding telescope pointing and acquiring UDP data stream

# Use ocs base image
FROM simonsobs/ocs:latest

# Install addition network related packages
RUN apt-get update -y && apt-get install -y iputils-ping \
    curl
# Set the working directory to copy your Agent into
#WORKDIR /home/sourav/ocs_prime_cam/wrkspace/agg_test/app/tcs_interface/
WORKDIR /app/ocs/ocs/agents/acu_interface/

# If there are extra dependencies install them here

# Copy the current directory into the WORKDIR
COPY . .

# Run registry on container startup
ENTRYPOINT ["dumb-init", "ocs-agent-cli"]

# Set default commandline arguments
CMD ["--agent", "agent.py", "--entrypoint", "main"]

