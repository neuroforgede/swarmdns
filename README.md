# swarmdns

SwarmDNS is a simple DNS server that can be used to resolve DNS queries for services running in a Docker Swarm cluster. It is designed to be used as a fallback to the built-in DNS resolution in Docker Swarm, which is based on the service name. SwarmDNS can be used to resolve DNS queries for services that are not part of the Docker Swarm cluster, or for services that are not reachable through the built-in DNS resolution for whatever reason.

This aims to solve the issues where the built-in DNS resolution in Docker Swarm does not resolve services due to bad network conditions that cause the networkdb to be corrupted. This can happen when the networkdb is not properly updated when services are created or removed, or when the networkdb is not properly replicated across the nodes in the cluster.

The original issue that inspired this project can be found here: https://github.com/moby/moby/issues/47728

## How it works

### The exporter

The exporter is a simple service that runs on each node in the Docker Swarm cluster. It exports information about the services and containers running on the node to a central S3 server.

### The nodes watcher

The nodes watcher is a simple service that runs once per cluster. It exports a list of relevant nodes in the cluster to a central S3 server. This is necessary for the merger to know which nodes to merge information from.

### The merger

The merger is a simple service that runs once per cluster. It merges the information from the exporters on each node in the Docker Swarm cluster into a single file that contains information about all the services and containers running in the cluster.

### The DNS server

SwarmDNS is a simple DNS server that listens for DNS queries. When a DNS query is received, SwarmDNS will determine the IP Address of the container that queried the DNS server. Then it will determine which services and containers are on shared networks with the container that queried the DNS server. Using this information it will resolve the DNS query.