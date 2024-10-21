#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2022 NeuroForge GmbH & Co. KG <https://neuroforge.de>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import docker
import os
import json
from datetime import datetime
from typing import Any
from threading import Event
import signal
from typing import Dict, List
import boto3

exit_event = Event()

def handle_shutdown(signal: Any, frame: Any) -> None:
    print_timed(f"received signal {signal}. shutting down...")
    exit_event.set()

signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)

EXPORTER_INTERVAL = int(os.getenv('EXPORTER_INTERVAL', '10'))

def print_timed(msg):
    to_print = '{} [{}]: {}'.format(
        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'exporter',
        msg)
    print(to_print)


def fetch_networks_and_subnets():
    """Fetch all Docker networks and their subnets."""
    client = docker.from_env()

    networks = client.networks.list()
    network_data = {}

    for network in networks:
        network_info = network.attrs
        network_id = network_info['Id']

        # Fetch subnets from IPAM config (IP Address Management)
        subnets = []
        ipam_config = network_info.get('IPAM', {}).get('Config', [])

        if ipam_config is None:
            ipam_config = []

        for config in ipam_config:
            subnet = config.get('Subnet', 'No Subnet')
            subnets.append(subnet)

        network_data[network_id] = {
            'subnets': subnets,
            'network_id': network_id,
            'name': network_info['Name'],
            'containers': [],
            'services': []  # Initialize services list for each network
        }

    return network_data

def fetch_containers_and_aliases(network_data):
    """Fetch all containers and their network aliases, organized by network."""
    client = docker.from_env()

    containers = client.containers.list()  # List all running containers

    # Loop over all containers
    for container in containers:
        container_info = container.attrs  # Fetch full container information
        container_name = container_info['Name'].strip('/')

        # Loop over each network the container is connected to
        networks = container_info['NetworkSettings']['Networks']
        for network_id, network_info in networks.items():
            ip_address = network_info.get('IPAddress', None)
            aliases = network_info.get('Aliases', [])
            dns_names = network_info.get('DNSNames', [])  # Fetch DNS names from network info
            container_id = container_info.get('Id', None)

            service = container_info.get('Config', {}).get('Labels', {}).get('com.docker.swarm.service.name', None)
            stack_name = container_info.get('Config', {}).get('Labels', {}).get('com.docker.stack.namespace', None)

            # If the network exists in our network_data (it should), add container info
            if network_id in network_data:
                network_data[network_id]['containers'].append({
                    'container_name': container_name,  # DNS name (container name),
                    'id': container_id,
                    'ip_address': ip_address,
                    'dns_names': dns_names,  # The DNS names
                    'aliases': aliases,
                    # add the service name so that we can match it to the service later
                    # so that we can do a lookup of the virtual IPs
                    'service': service,
                    'stack_name': stack_name
                })

    return network_data

def fetch_and_attach_swarm_services(network_data):
    """Fetch Docker Swarm services and attach them to their respective networks."""
    client = docker.from_env()

    # Check if Docker Swarm is enabled
    try:
        swarm_info = client.info().get('Swarm', {})
        if swarm_info.get('LocalNodeState') != 'active':
            print_timed("Docker Swarm is not enabled.")
            return network_data
    except Exception as e:
        print_timed(f"Error checking Docker Swarm mode: {e}")
        return network_data

    try:
        services = client.services.list()  # Fetch all Swarm services
    except docker.errors.APIError as e:
        print_timed(f"Error fetching Swarm services: {e}")
        return network_data

    # Attach services to the appropriate networks
    for service in services:
        service_info = service.attrs
        service_name = service_info.get('Spec', {}).get('Name', 'unknown')
        replicas = service_info.get('Spec', {}).get('Mode', {}).get('Replicated', {}).get('Replicas', None)
        networks = service_info.get('Spec', {}).get('TaskTemplate', {}).get('Networks', [])
        virtual_ips = service_info.get('Endpoint', {}).get('VirtualIPs', [])
        endpoint_mode = service_info.get('Spec', {}).get('EndpointSpec', {}).get('Mode', 'vip')
        service_id = service_info.get('ID', None)
        stack_name = service_info.get('Spec', {}).get('Labels', {}).get('com.docker.stack.namespace', None)

        # Attach the service to each network it belongs to
        for network in networks:
            network_id = network.get('Target', '')
            aliases = network.get('Aliases', [])
            # Match network ID to network names in network_data
            for network_id, network_info in network_data.items():
                if network_id == network_info.get('network_id'):  # Match by network ID
                    vip_list = [vip.get('Addr', '') for vip in virtual_ips if vip.get('NetworkID') == network_id]
                    # remove /24 etc from the end of the IP
                    vip_list = [vip.split('/')[0] for vip in vip_list]
                    network_info['services'].append({
                        'service_id': service_id,
                        'service_name': service_name,
                        'replicas': replicas,
                        'aliases': aliases,
                        'endpoint_mode': endpoint_mode,
                        'virtual_ips': vip_list,  # Add Virtual IPs to the service info,
                        'stack_name': stack_name
                    })
                    break

    return network_data

def save_network_data_to_json(network_data, filename):
    """Save the network data to a JSON file."""
    with open(filename, 'w') as f:
        json.dump(network_data, f, indent=4)
    print_timed(f"Network data saved to {filename}")


def upload_to_dns_s3(data, bucket, object_name=None):
    """Upload a file to S3 bucket.

    :param data: File to upload
    :param bucket: Bucket to upload to
    :param object_name: S3 object name. If not specified then file_name is used
    :return: True if file was uploaded, else False
    """
    # S3 configuration
    dns_s3_endpoint = os.environ['DNS_S3_ENDPOINT']
    dns_s3_access_key = os.environ['DNS_S3_ACCESS_KEY']
    dns_s3_secret_key = os.environ['DNS_S3_SECRET_KEY']

    # Create the S3 client with custom endpoint
    s3_client = boto3.client('s3',
                             endpoint_url=dns_s3_endpoint,
                             aws_access_key_id=dns_s3_access_key,
                             aws_secret_access_key=dns_s3_secret_key)

    try:
        s3_client.put_object(Bucket=bucket, Key=object_name, Body=json.dumps(data, indent=4))
        print_timed(f"File {object_name} uploaded to {bucket}/{object_name} on S3")
    except Exception as e:
        print_timed(f"Error uploading {object_name} to S3: {e}")
        return False
    return True


if __name__ == '__main__':
    while not exit_event.is_set():
        # Fetch networks and subnets first
        network_data = fetch_networks_and_subnets()

        # Fetch Docker Swarm services and attach them to the networks
        network_data = fetch_and_attach_swarm_services(network_data)

        # Fetch containers, DNS names, and aliases, and integrate into network data
        network_data = fetch_containers_and_aliases(network_data)

        filename = os.environ['SWARM_NODE_ID']
        bucket_name = os.environ['DNS_S3_BUCKET_NAME']
        upload_to_dns_s3(network_data, bucket_name, object_name=f"node-data/{filename}.json")

        exit_event.wait(EXPORTER_INTERVAL)