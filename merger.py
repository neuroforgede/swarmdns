import boto3
import os
import json
from datetime import datetime
from threading import Event
import signal
from typing import Any

exit_event = Event()

def handle_shutdown(signal: Any, frame: Any) -> None:
    print_timed(f"received signal {signal}. shutting down...")
    exit_event.set()

signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


SCRAPE_INTERVAL = int(os.getenv('SCRAPE_INTERVAL', '10'))
MAX_RETRIES_IN_ROW = int(os.getenv('MAX_RETRIES_IN_ROW', '10'))

def print_timed(msg):
    """Print a message with a timestamp for better debugging."""
    to_print = '{} [{}]: {}'.format(
        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'docker_events',
        msg)
    print(to_print)

def load_json_from_s3(bucket, key):
    """Download and load a JSON file from S3."""
    print_timed(f"Attempting to load JSON from S3: {key}")
    s3_client = boto3.client('s3',
                             endpoint_url=os.getenv('DNS_S3_ENDPOINT'),
                             aws_access_key_id=os.getenv('HETZNER_ACCESS_KEY'),
                             aws_secret_access_key=os.getenv('HETZNER_SECRET_KEY'))
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        content = response['Body'].read().decode('utf-8')
        print_timed(f"Successfully loaded {key} from S3")
        return json.loads(content)
    except Exception as e:
        print_timed(f"Error loading {key} from S3: {e}")
        return None

def save_json_to_s3(data, bucket, key):
    """Upload a JSON file to S3."""
    print_timed(f"Attempting to upload JSON to S3: {key}")
    s3_client = boto3.client('s3',
                             endpoint_url=os.getenv('DNS_S3_ENDPOINT'),
                             aws_access_key_id=os.getenv('HETZNER_ACCESS_KEY'),
                             aws_secret_access_key=os.getenv('HETZNER_SECRET_KEY'))
    try:
        s3_client.put_object(Bucket=bucket, Key=key, Body=json.dumps(data, indent=4))
        print_timed(f"Successfully uploaded {key} to S3")
    except Exception as e:
        print_timed(f"Error uploading {key} to S3: {e}")

def merge_data(merged_data, node_data):
    """Merge node data into the merged data, avoiding duplicates using 'id' for containers and 'service_id' for services."""
    for network_name, network_info in node_data.items():
        # Initialize the network in merged data if it doesn't exist
        if network_name not in merged_data:
            merged_data[network_name] = {
                "subnets": [],
                "containers": [],
                "services": []
            }
            print_timed(f"Created new network entry for {network_name}")

        # Merge subnets
        initial_subnets_count = len(merged_data[network_name]["subnets"])
        merged_data[network_name]["subnets"] = list(set(merged_data[network_name]["subnets"] + network_info["subnets"]))
        print_timed(f"Merged subnets for network {network_name}. Added {len(merged_data[network_name]['subnets']) - initial_subnets_count} new subnets.")

        # Merge containers (avoiding duplicates by 'id')
        container_ids = {c["id"] for c in merged_data[network_name]["containers"]}
        for container in network_info["containers"]:
            if container["id"] not in container_ids:
                merged_data[network_name]["containers"].append(container)
                container_ids.add(container["id"])
                print_timed(f"Added container {container['container_name']} (ID: {container['id']}) to network {network_name}")

        # Merge services (avoiding duplicates by 'service_id')
        service_ids = {s["service_id"] for s in merged_data[network_name]["services"]}
        for service in network_info["services"]:
            if service["service_id"] not in service_ids:
                merged_data[network_name]["services"].append(service)
                service_ids.add(service["service_id"])
                print_timed(f"Added service {service['service_name']} (ID: {service['service_id']}) to network {network_name}")

    return merged_data

def clean_up_old_files(bucket, valid_files):
    """Remove files from S3 'node-data' folder that are not in the valid_files list."""
    print_timed(f"Starting cleanup of old files in node-data folder.")
    s3_client = boto3.client('s3',
                             endpoint_url=os.getenv('DNS_S3_ENDPOINT'),
                             aws_access_key_id=os.getenv('HETZNER_ACCESS_KEY'),
                             aws_secret_access_key=os.getenv('HETZNER_SECRET_KEY'))
    try:
        response = s3_client.list_objects_v2(Bucket=bucket, Prefix="node-data/")
        if "Contents" in response:
            for item in response["Contents"]:
                file_key = item["Key"]
                file_name = os.path.basename(file_key)
                if file_name not in valid_files:
                    # Delete the file if it's not listed in the valid files
                    s3_client.delete_object(Bucket=bucket, Key=file_key)
                    print_timed(f"Deleted old file: {file_key}")
                else:
                    print_timed(f"File {file_key} is still valid, skipping deletion.")
        else:
            print_timed("No files found in node-data folder for cleanup.")
    except Exception as e:
        print_timed(f"Error cleaning up old files: {e}")

def run_merge():
    bucket_name = os.environ['DNS_S3_BUCKET_NAME']
    
    # Step 1: Load nodes.json from S3
    print_timed("Starting to load nodes.json from S3")
    nodes = load_json_from_s3(bucket_name, 'nodes.json')
    if not nodes or 'nodes' not in nodes:
        print_timed("No valid nodes data found in nodes.json.")
        return

    # Initialize an empty merged data structure
    merged_data = {}

    # Step 2: Loop through each node, loading its node data
    valid_node_files = []
    for node in nodes["nodes"]:
        node_id = node["id"]
        valid_node_files.append(f"{node_id}.json")
        print_timed(f"Loading data for node: {node_id}")
        node_data = load_json_from_s3(bucket_name, f"node-data/{node_id}.json")
        
        if node_data:
            print_timed(f"Merging data for node: {node_id}")
            # Step 3: Merge node data into the final structure
            merged_data = merge_data(merged_data, node_data)
        else:
            print_timed(f"No data found for node: {node_id}, skipping.")

    # Step 4: Save the merged data to S3
    print_timed("Saving merged data to S3")
    save_json_to_s3(merged_data, bucket_name, 'network_data.json')

    # Step 5: Clean up old files in the 'node-data' folder
    print_timed("Cleaning up old files in node-data folder")
    clean_up_old_files(bucket_name, valid_node_files)

if __name__ == "__main__":
    while not exit_event.is_set():
        run_merge()

        exit_event.wait(SCRAPE_INTERVAL)