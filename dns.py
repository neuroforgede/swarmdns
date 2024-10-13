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

import socket
from dnslib import DNSRecord, DNSHeader, RR, A
import threading

import docker
import json
from datetime import datetime
from typing import Any
from threading import Event
import signal
from typing import Dict, List
import os
import boto3
from concurrent.futures import ThreadPoolExecutor


STRIP_DOMAIN_ENDINGS = os.getenv('STRIP_DOMAIN_ENDINGS', '.localdomain.,.docker.,.docker.localdomain.').split(',')
DEBUG = os.getenv('DEBUG', 'false').lower() == 'true'
S3_REFRESH_INTERVAL = int(os.getenv('S3_REFRESH_INTERVAL', '10'))
DOCKER_NETWORK_INFO_CACHE_REFRESH_INTERVAL = int(os.getenv('DOCKER_NETWORK_INFO_CACHE_REFRESH_INTERVAL', '10'))

def print_debug(msg):
    if DEBUG:
        print(msg)

def print_timed(msg):
    to_print = '{} [{}]: {}'.format(
        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'dns',
        msg)
    print(to_print)

exit_event = Event()
s3_network_data = {}  # Cache for network data loaded from S3
container_ip_mapping = {}  # Global mapping of container ID to IP address
network_info_cache = {}  # Cache for network information

def handle_shutdown(signal: Any, frame: Any) -> None:
    print_timed(f"received signal {signal}. shutting down...")
    exit_event.set()

signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)



def refresh_network_data_from_s3():
    global s3_network_data
    bucket_name = os.environ['DNS_S3_BUCKET_NAME']
    filename = 'network_data.json'

    print_debug("Refreshing network data from S3...")
    try:
        s3_network_data = load_network_data_from_dns_s3(bucket_name, filename)
        print_debug(f"S3 network data refreshed: {s3_network_data}")
    except Exception as e:
        print_timed(f"Error refreshing network data from S3: {e}")



def refresh_network_data_from_s3_thread_worker():
    while not exit_event.is_set():
        refresh_network_data_from_s3()
        threading.Event().wait(S3_REFRESH_INTERVAL)  # Refresh every S3_REFRESH_INTERVAL seconds


def refresh_network_info_cache():
    global network_info_cache

    print_debug("Refreshing network info cache...")
    try:
        network_info_cache = fetch_networks_info()
    except Exception as e:
        print_timed(f"Error refreshing network info cache: {e}")

def refresh_network_info_cache_thread_worker():
    while not exit_event.is_set():
        refresh_network_info_cache()
        threading.Event().wait(DOCKER_NETWORK_INFO_CACHE_REFRESH_INTERVAL)  # Refresh every 10 seconds

def listen_to_docker_events():
    """
    Listen to Docker events and update container IP mapping when containers are started/stopped/connected/disconnected.
    """
    while not exit_event.is_set():
        try:
            # if something crashed start with a refresh
            refresh_network_info_cache()

            # global refresh, not perfect, instead we should only update
            # the things that are actually affected
            client = docker.from_env()
            for event in client.events(decode=True):
                if event['Type'] == 'container':
                    action = event['Action']
                    if action in ['start', 'connect']:
                        refresh_network_info_cache()
                    elif action in ['stop', 'disconnect', 'die']:
                        refresh_network_info_cache()
        except Exception as e:
            print_timed(f"Error listening to Docker events: {e}")


def fetch_networks_info():
    """
    Fetches all networks and their associated containers' network information.
    Returns a dictionary where the key is the network name and the value is
    another dictionary containing container IDs and their IPs.
    """
    client = docker.from_env()
    networks = client.networks.list()

    network_info = {}

    for network in networks:
        network_id = network.attrs["Id"]

        # get network details by id
        network_details = client.api.inspect_network(network_id)
        # print(network_details)
        network_name = network_details["Name"]
        
        containers = network_details["Containers"]
        network_info[network_name] = {}

        for container_id, container_details in containers.items():
            ip_address = container_details.get("IPv4Address")
            if ip_address:
                # Strip subnet from the IP address (since it's in CIDR format)
                ip_address = ip_address.split('/')[0]
                network_info[network_name][container_id] = ip_address
    
    return network_info

def find_container_id_from_ip(ip_address):
    """
    Takes an IP address and checks all networks to determine which container it belongs to.
    Returns the container ID, or None if not found.
    """
    global network_info_cache
    network_info = network_info_cache

    for network_name, container_info in network_info.items():
        for container_id, container_ip in container_info.items():
            if container_ip == ip_address:
                return container_id
    
    return None


def get_networks_from_container_id(container_id):
    """
    Takes a container ID and returns a list of networks that the container is connected to.
    """
    client = docker.from_env()
    container = client.containers.get(container_id)
    network_info = container.attrs["NetworkSettings"]["Networks"]

    networks = []
    for network_name, network_details in network_info.items():
        networks.append(network_name)
    
    return networks


def load_network_data_from_dns_s3(bucket, object_name):
    """
    Load network data from a JSON file stored in S3.
    
    :param bucket: The S3 bucket name
    :param object_name: The name of the object (file) in the S3 bucket
    :return: The loaded network data as a dictionary, or an empty dictionary if the file is not found
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
        # Fetch the object from S3
        response = s3_client.get_object(Bucket=bucket, Key=object_name)
        
        # Read the content of the file (it's in bytes, so we need to decode it)
        file_content = response['Body'].read().decode('utf-8')
        
        # Parse the JSON content
        network_data = json.loads(file_content)
        
        return network_data
    except s3_client.exceptions.NoSuchKey:
        print_timed(f"File {object_name} not found in bucket {bucket}. Returning empty dictionary.")
        return {}
    except Exception as e:
        print_timed(f"Error fetching {object_name} from S3: {e}")
        return {}

def get_network_data():
    """
    Load network data from a JSON file.
    """
    global s3_network_data
    return s3_network_data

def resolve_dnsA_to_ip(network_data, networks, domain):
    """
    Resolves DNS A records to IP addresses based on the network data.
    """
    network_data = get_network_data()

    print_debug(f"Resolving DNS A records for domain: {domain}")
    print_debug(f"Networks: {networks}")

    dnsA_records = set()

    for network_name, network_info in network_data.items():
        if network_name not in networks:
            continue

        if 'containers' in network_info:
            for container_data in network_info['containers']:
                ip_address = container_data['ip_address']

                service_name = container_data['service']
                if f'tasks.{service_name}' == domain:
                    dnsA_records.add(ip_address)

                dns_names = container_data.get('dns_names', [])
                if dns_names is not None:
                    for dns_name in dns_names:
                        if dns_name == domain:
                            dnsA_records.add(ip_address)
                
                aliases = container_data.get('aliases', [])
                if aliases is not None:
                    for alias in aliases:
                        if alias == domain:
                            dnsA_records.add(ip_address)

        if 'services' in network_info:
            for service_data in network_info['services']:
                relevant_ips = service_data['virtual_ips']
                endpoint_mode = service_data['endpoint_mode']
                service_name = service_data['service_name']

                if endpoint_mode == 'dnsrr':
                    service_name = service_data['service_name']
                    ip_addresses = set()

                    # find containers that are part of the service
                    for container_data in network_info['containers']:
                        if container_data['service'] == service_name:
                            ip_address = container_data['ip_address']
                            ip_addresses.add(ip_address)
                    
                    relevant_ips = list(ip_addresses)

                if service_name == domain:
                    for ip in relevant_ips:
                        dnsA_records.add(ip)

                dns_names = service_data.get('dns_names', [])
                if dns_names is not None:
                    for dns_name in dns_names:
                        if dns_name == domain:
                            for ip in relevant_ips:
                                dnsA_records.add(ip)
                
                aliases = service_data.get('aliases', [])
                if aliases is not None:
                    for alias in aliases:
                        if alias == domain:
                            for ip in relevant_ips:
                                dnsA_records.add(ip)

    print_debug(f"Resolved DNS A records: {dnsA_records}")
    return list(dnsA_records)
    
# DNS Server
class DNSServer:
    def __init__(self, ip="0.0.0.0", port=53):
        print_debug("Initializing DNS server on {ip}:{port}")
        self.ip = ip
        self.port = port
        self.server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)  # Increase buffer size
        self.server.bind((self.ip, self.port))
        self.executor = ThreadPoolExecutor(max_workers=10)  # Limit the number of concurrent workers
        print_debug(f"DNS server initialized on {self.ip}:{self.port}")

    def handle_request(self, data, addr):
        # Parse incoming DNS request
        request = DNSRecord.parse(data)
        print_debug(f"Received request from {addr}: {request.q.qname}")

        # Create DNS response header
        reply = DNSRecord(DNSHeader(id=request.header.id, qr=1, aa=1, ra=0), q=request.q)

        original_domain = str(request.q.qname)

        # Find the domain in the DNS_TABLE and return the corresponding IP address
        domain = original_domain

        # strip the trailing .localdomain
        for ending in STRIP_DOMAIN_ENDINGS:
            if domain.endswith(ending):
                domain = domain[:-len(ending)]
                break

        request_addr = addr[0]
        container_id = find_container_id_from_ip(request_addr)

        if container_id:
            print_debug(f"IP {request_addr} belongs to container {container_id}.")

            # Get the networks that the container is connected to
            networks = get_networks_from_container_id(container_id)
            
            # Load network data
            network_data = get_network_data()

            # Resolve DNS A records to IP addresses
            dnsA_records = resolve_dnsA_to_ip(network_data, networks, domain)

            for ip in dnsA_records:
                reply.add_answer(RR(original_domain, rdata=A(ip)))
                print_debug(f"Added response for {original_domain} -> {ip}")

        else:
            print_debug(f"IP {request_addr} not found in any Docker network.")

        # Send the DNS response
        self.server.sendto(reply.pack(), addr)
        print_debug(f"Response sent to {addr}")

    def start(self):
        print_timed(f"DNS Server running on {self.ip}:{self.port}...")
        while not exit_event.is_set():
            try:
                data, addr = self.server.recvfrom(512)
                self.executor.submit(self.handle_request, data, addr)  # Submit tasks to the thread pool
            except Exception as e:
                print_timed(f"Error: {e}")

    def stop(self):
        print_timed("Shutting down DNS server...")
        self.server.close()
        self.executor.shutdown(wait=True)
        print_debug("DNS server shutdown complete")

if __name__ == '__main__':
    # save_network_data_to_json(dns_to_ip_mapping, 'dns_to_ip.json')
    print("Starting DNS server...")

    refresh_network_data_from_s3()
    refresh_network_info_cache()

    # Start network data refresher from S3
    threading.Thread(target=refresh_network_data_from_s3_thread_worker, daemon=True).start()

    # Start thread to fresh network info cache (as a fallback)
    threading.Thread(target=refresh_network_info_cache_thread_worker, daemon=True).start()

    # Start listening to Docker events
    threading.Thread(target=listen_to_docker_events, daemon=True).start()

    bind_ip = None
    try:
        # get bind ip by asking the docker api what the ip of the node is
        # in docker swarm
        client = docker.from_env()
        node_info = client.info()
        node_ip = node_info['Swarm']['NodeAddr']
        bind_ip = node_ip
    except Exception as e:
        print_timed(f"Error getting node IP from Docker API. Falling back to env var BIND_IP {e}")
        bind_ip = os.environ['BIND_IP']

    # Initialize DNS server
    server = DNSServer(ip=bind_ip, port=53)

    # Start the DNS server in the main thread
    server.start()

    # Wait for the shutdown event
    exit_event.wait()

    print_timed("Server has shut down gracefully.")