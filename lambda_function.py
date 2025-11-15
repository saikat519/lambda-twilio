import os
import json
import time
import base64
from datetime import datetime
from typing import List, Dict, Any, Optional
import boto3
from urllib.parse import parse_qs,quote_plus,urlparse
import urllib.parse
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather, Say
from botocore.exceptions import ClientError, NoCredentialsError, ParamValidationError

// add yours
account_sid = '' 
auth_token = ''
from_number = ''
s3_bucket_name = ''
public_twiml_url = ''
public_twiml_callback_url = ''
target_phone_number = ''
max_call_attempts = 5


s3 = boto3.client('s3')
twilio_client = Client(account_sid, auth_token)


def get_incident_state_from_s3(ticket_id: str,folder_name: str) -> Optional[Dict[str, Any]]:
    try:
        response = s3.get_object(Bucket=s3_bucket_name, Key=f'twilio/{folder_name}/{ticket_id}.json')
        return json.loads(response['Body'].read().decode('utf-8'))
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey':
            return None
        else:
            raise

def store_incident_state_in_s3(incident_data: str) -> None:
    ticket_id = incident_data['ticket_id']
    try:
        if incident_data['status'] == 'ACKNOWLEDGED':
            s3.put_object(Bucket=s3_bucket_name, 
            Key=f'twilio/acknowledged/{ticket_id}.json', 
            Body=json.dumps(incident_data),
            ContentType='application/json'
            )
            s3.delete_object(Bucket=s3_bucket_name,
            Key=f'twilio/pending/{ticket_id}.json'
            )
            print(f"Stored acknowledged incident state for ticket {ticket_id} and removed from pending folder")
        else:
            s3.put_object(Bucket=s3_bucket_name,
            Key=f'twilio/pending/{ticket_id}.json',
            Body=json.dumps(incident_data),
            ContentType='application/json'
            )
            print(f"Stored incident state for ticket {ticket_id}")
    except (ClientError, NoCredentialsError, ParamValidationError) as e:
        print(f"Error storing incident state in S3: {e}")
        raise

def make_outbound_call(target_phone_number: str,incident_data) -> None:
    try:
        print(f"Making outbound call to {target_phone_number} with summary {incident_data['summary']}")
        params = {
            "ticket_id": incident_data['ticket_id'],
            "summary": incident_data['summary'],
        }

        encoded = urllib.parse.urlencode(params)

        twiml_url = f"{public_twiml_url}?{encoded}"
        call = twilio_client.calls.create(
            to=target_phone_number,
            from_=from_number,
            url=twiml_url
        )
        print(f"Outbound call initiated to {target_phone_number} with TwiML URL {twiml_url}. Call SID: {call.sid}")
        return call.sid
    except (ClientError, NoCredentialsError, ParamValidationError) as e:
        print(f"Error making outbound call: {e}")
        return None

def run_escalation_loop(ticket_id, summary, incident_data):
    print("start escalation loop",incident_data,summary)
    incident_data = get_incident_state_from_s3(ticket_id, 'pending')
    if not incident_data:
        incident_data={
            'ticket_id': ticket_id,
            'status': 'PENDING',
            'summary': summary,
            'target_numbers': target_phone_number.split(','),
            'call_attempts': 0,
            'timestamp': datetime.now().isoformat()
        }
        store_incident_state_in_s3(incident_data)
    print("before escalation loop", incident_data)
    while incident_data['call_attempts'] < max_call_attempts:
        incident_data=get_incident_state_from_s3(ticket_id, 'acknowledged')
        if incident_data and incident_data['status'] == 'ACKNOWLEDGED':
            print(f"Incident {ticket_id} has been acknowledged. Exiting escalation loop.")
            return {"status": "ACKNOWLEDGED"}
        else:
            incident_data=get_incident_state_from_s3(ticket_id, 'pending')
        
        print(f"Call attempt {incident_data['call_attempts'] + 1} for incident {ticket_id}")
        current_call_sids = []
        for target_number in incident_data['target_numbers']:
            call_sid = make_outbound_call(target_number, incident_data)
            if call_sid:
                current_call_sids.append(call_sid)
        incident_data['call_attempts'] += 1
        store_incident_state_in_s3(incident_data)

        if incident_data['call_attempts'] >= max_call_attempts:
            print(f"Maximum call attempts reached for incident {ticket_id}. Exiting escalation loop.")
            return {"status": "MAX_ATTEMPTS_REACHED"}
        print(f"sleeping for 120 seconds for acknowledgement for incident {ticket_id}")
        time.sleep(120)
    print("Escalation finished ticket id:",ticket_id)
    return {"status": incident_data.get('status','UNKNOWN')}

def handle_incident_trigger(event_body):
    try:
        if isinstance(event_body, str):
            try:
                body = json.loads(event_body)
            except json.JSONDecodeError:
                raise ValueError("Invalid JSON in event body")
        elif isinstance(event_body, dict):
            body = event_body
        else:
            raise ValueError("Event body must be a JSON string or a dictionary")
        incident_data = get_incident_state_from_s3(body['ticket_id'], 'acknowledged')
        if incident_data and incident_data['status'] == 'ACKNOWLEDGED':
            print(f"Incident {body['ticket_id']} is already acknowledged. Ignoring trigger.")
            return {"status": "ACKNOWLEDGED"}
        print("escalation BODY>>>>",body)
        escalation_result = run_escalation_loop(body['ticket_id'],body['summary'], body)
        return {
            "statusCode": 200,
            "body": json.dumps({"messege":"process initiateddd","final_status":escalation_result['status']})
        }
    except (ValueError, PermissionError, ClientError) as e:
        print(f"Error handling incident trigger: {e}")
        return {
            "statusCode": 400,
            "body": json.dumps({"error": str(e)})
        }
    except Exception as e:
        print(f"Unexpected error in handle_incident_trigger: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "An unexpected error occurred"})
        }

def handle_twiml(event):
    print("event in handle GET call>>>>:", event)
    ticket_id = event.get("queryStringParameters", {}).get("ticket_id", "")
    summary = event.get("queryStringParameters", {}).get("summary", "")
    
    twiml = f"""
        <Response>
            <Gather numDigits="1" action="/acknowledge" method="POST">
                <Say>
                    Hi this is an alert call for ticket number {ticket_id}. 
                    summary {summary}
                    Press 1 to acknowledge.
                    Press 2 to repeat.
                </Say>
            </Gather>
            <Say>No input received. Goodbye.</Say>
            <Hangup/>
        </Response>
    """

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/xml"},
        "body": twiml
    }      



def handle_acknowledge(event):
    print("RAW EVENT:", event)

    body = event.get("body", "")
    is_base64 = event.get("isBase64Encoded", False)

    # Decode if base64
    if is_base64:
        print("Decoding Base64...")
        body = base64.b64decode(body).decode("utf-8")

    print("DECODED BODY:", body)

    params = parse_qs(body)
    print("PARSED:", params)

    digits = params.get("Digits", [""])[0]
    print("DIGITS:", digits)

    headers = event.get("headers", {})
    # Twilio sometimes sends lowercase or capitalized
    referer_url = headers.get("referer") or headers.get("Referer")
    # Parse the URL
    parsed_url = urlparse(referer_url)
    query_params = parse_qs(parsed_url.query)
    # Extract only ticket_id
    ticket_id = query_params.get("ticket_id", [None])[0]
    summary = query_params.get("summary", [None])[0]

    print("Ticket ID:", ticket_id)

    if digits == "1":

        incident_data = get_incident_state_from_s3(ticket_id, 'pending')
        if incident_data:
            incident_data['status'] = 'ACKNOWLEDGED'
            incident_data['timestamp'] = datetime.now().isoformat()
            store_incident_state_in_s3(incident_data)
        twiml = """
        <Response>
            <Say>Thank you. Your acknowledgement is recorded.</Say>
            <Hangup/>
        </Response>
        """

    elif digits == "2":
        twiml = f"""
        <Response>
            <Gather numDigits="1" action="/acknowledge" method="POST">
                <Say>
                    Hi this is an alert call for ticket number {ticket_id}.
                    summary {summary}
                    Press 1 to acknowledge.
                    Press 2 to repeat.
                </Say>
            </Gather>
            <Say>No input received. Goodbye.</Say>
            <Hangup/>
        </Response>
        """

    else:
        twiml = """
        <Response>
            <Say>Invalid input. Goodbye.</Say>
            <Hangup/>
        </Response>
        """

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/xml"},
        "body": twiml
    }


def lambda_handler(event, context):
    print("Event:", event)

    path = event.get("rawPath", "/")
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    

    # ---- 2. Provide TwiML Instructions (Twilio → Lambda) ----
    if "twiml" in path:
        print("twiml GET call>>>>>>>",path)
        return handle_twiml(event)

    # ---- 3. Handle Acknowledgement (Twilio Gather) ----
    if "acknowledge" in path:
        print("acknowledge POST call>>>>>>>",path)
        return handle_acknowledge(event)
    else:
        print("Direct invocation",event)
        return handle_incident_trigger(event)

    return {
        "statusCode": 404,
        "body": "Unknown route"
    }
