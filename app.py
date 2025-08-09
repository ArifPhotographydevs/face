from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import boto3
import base64
import face_recognition
import os
import tempfile
import uuid
import concurrent.futures
import logging
from PIL import Image, UnidentifiedImageError
from botocore.config import Config
from zipfile import ZipFile
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ===== LOGGING =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ===== CONFIG (HARDCODED AS REQUESTED) =====
ACCESS_KEY = "SW5I2XCNJAI7GTB7MRIW"
SECRET_KEY = "eKNEI3erAhnSiBdcK0OltkTHIe2jJYJVhPu1eazJ"
REGION = "ap-northeast-1"
BUCKET = "arif12"
ENDPOINT_URL = f"https://s3.{REGION}.wasabisys.com"
BOTO_CONFIG = Config(max_pool_connections=30)

# EMAIL (hard-coded)
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = "githubarifphotography@gmail.com"
SENDER_PASSWORD = "utuz rvgk kmsv sntz"  # App password or real password

# Matching hyperparams
IMAGE_MAX_SIZE = (800, 800)   # reduce size for faster encoding
MAX_WORKERS = min(4, (os.cpu_count() or 2) - 1 if (os.cpu_count() or 2) > 1 else 1)
MAX_MATCHES = 200             # stop after finding this many matches (to limit work)
FACE_COMPARE_TOLERANCE = 0.6  # lower is stricter (0.6 is typical)

# ===== S3 CLIENT (for main process) =====
s3 = boto3.client(
    "s3",
    aws_access_key_id=ACCESS_KEY,
    aws_secret_access_key=SECRET_KEY,
    region_name=REGION,
    endpoint_url=ENDPOINT_URL,
    config=BOTO_CONFIG
)

# ===== FLASK APP =====
app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)


# ===== Helper utils =====
def safe_resize_image(path, max_size=IMAGE_MAX_SIZE):
    """Resize image in-place to speed up face encoding."""
    try:
        with Image.open(path) as img:
            # choose good resample
            if hasattr(Image, "Resampling"):
                resample = Image.Resampling.LANCZOS
            else:
                resample = Image.LANCZOS
            img.thumbnail(max_size, resample)
            if img.mode in ('RGBA', 'LA', 'P'):
                img = img.convert('RGB')
            img.save(path)
    except Exception as e:
        logger.warning(f"safe_resize_image failed for {path}: {e}")


def _create_s3_client_for_worker():
    """Create a fresh boto3 client inside worker process (avoid pickling client)."""
    return boto3.client(
        "s3",
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name=REGION,
        endpoint_url=ENDPOINT_URL,
        config=Config(max_pool_connections=2)
    )


def download_to_tmp(key):
    """Download S3 object to a temp file and return local path."""
    fd, local_path = tempfile.mkstemp(suffix=os.path.splitext(key)[1] or ".jpg")
    os.close(fd)
    try:
        s3.download_file(Bucket=BUCKET, Key=key, Filename=local_path)
        return local_path
    except Exception as e:
        logger.warning(f"Failed to download {key}: {e}")
        try:
            if os.path.exists(local_path):
                os.unlink(local_path)
        except:
            pass
        return None


# The worker function MUST be top-level (picklable)
def download_and_compare_worker(args):
    """
    Worker that downloads a single image from S3, resizes, computes encodings and compares
    to the known_encoding provided. Returns (key, local_path) on match, else None.
    args: tuple(key, known_encoding, tolerance)
    """
    key, known_encoding, tolerance = args
    # create a local s3 client per worker to avoid sharing sockets
    local_s3 = _create_s3_client_for_worker()
    local_tmp = None
    try:
        # create a unique temp file
        fd, local_tmp = tempfile.mkstemp(suffix=os.path.splitext(key)[1] or ".jpg")
        os.close(fd)
        # download
        local_s3.download_file(Bucket=BUCKET, Key=key, Filename=local_tmp)

        # ensure valid image
        try:
            with Image.open(local_tmp) as img:
                img.verify()
        except Exception:
            # invalid image, skip
            try:
                os.unlink(local_tmp)
            except:
                pass
            return None

        # resize to accelerate encoding
        safe_resize_image(local_tmp)

        # load and encode
        image = face_recognition.load_image_file(local_tmp)
        encs = face_recognition.face_encodings(image)
        if not encs:
            # no face found
            try:
                os.unlink(local_tmp)
            except:
                pass
            return None

        # compare: if any face in file matches known_encoding within tolerance => matched
        for enc in encs:
            # face_distance lower means more similar; compare_faces uses tolerance
            matches = face_recognition.compare_faces([known_encoding], enc, tolerance=tolerance)
            if matches and matches[0]:
                # return key and path of matched file
                return {"key": key, "local_path": local_tmp}
        # not matched
        try:
            os.unlink(local_tmp)
        except:
            pass
        return None
    except Exception as e:
        logger.exception(f"Worker exception for {key}: {e}")
        if local_tmp and os.path.exists(local_tmp):
            try:
                os.unlink(local_tmp)
            except:
                pass
        return None


def create_presigned_url(bucket, key, expiration=3600):
    try:
        return s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket, 'Key': key},
            ExpiresIn=expiration
        )
    except Exception as e:
        logger.warning(f"Presigned URL creation failed for {key}: {e}")
        return None


def send_link_email(download_url, recipient_email, name, phone):
    try:
        msg = MIMEMultipart()
        msg["From"] = SENDER_EMAIL
        msg["To"] = recipient_email
        msg["Subject"] = f"Face Recognition ZIP Download Link for {name}"
        body = f"""
Hi {name},

Thank you for using the Face Matching service.
Phone: {phone}

Here is your temporary download link for all matched images (valid for 1 hour):
{download_url}

If you have issues downloading, please contact support.

Best,
Face Matcher Bot
"""
        msg.attach(MIMEText(body, "plain"))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, recipient_email, msg.as_string())
        server.quit()
        logger.info(f"Sent download link to {recipient_email}")
    except Exception as e:
        logger.exception(f"Failed to send email to {recipient_email}: {e}")


# ===== ROUTES =====
@app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')


@app.route('/match', methods=['POST', 'OPTIONS'])
def match_face():
    if request.method == 'OPTIONS':
        return jsonify({}), 200, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type'
        }

    try:
        data = request.get_json(force=True)
        for field in ["selfie", "email", "name", "phone"]:
            if not data.get(field):
                return jsonify({"success": False, "message": f"Missing {field}"}), 400, {
                    'Access-Control-Allow-Origin': '*'
                }

        name = data["name"]
        recipient_email = data["email"]
        phone = data["phone"]

        # Save selfie to temp, validate and encode once
        selfie_bytes = base64.b64decode(data["selfie"])
        selfie_fd, selfie_path = tempfile.mkstemp(suffix=".jpg")
        os.close(selfie_fd)
        with open(selfie_path, "wb") as f:
            f.write(selfelfie := selfie_bytes)

        # validate image
        try:
            with Image.open(selfie_path) as test_img:
                test_img.verify()
        except (UnidentifiedImageError, Exception) as e:
            logger.warning(f"Uploaded selfie is not a valid image: {e}")
            try:
                os.unlink(selfie_path)
            except:
                pass
            return jsonify({"success": False, "message": "Uploaded selfie is not a valid image."}), 400, {
                'Access-Control-Allow-Origin': '*'
            }

        # resize selfie and compute encoding
        safe_resize_image(selfie_path)
        known_image = face_recognition.load_image_file(selfie_path)
        known_encodings = face_recognition.face_encodings(known_image)
        try:
            os.unlink(selfie_path)
        except:
            pass

        if not known_encodings:
            return jsonify({"success": False, "message": "No face found in selfie"}), 400, {
                'Access-Control-Allow-Origin': '*'
            }
        known_encoding = known_encodings[0]

        # List images in bucket (use paginator)
        logger.info("Listing objects in bucket...")
        paginator = s3.get_paginator('list_objects_v2')
        page_iterator = paginator.paginate(Bucket=BUCKET)
        image_keys = []
        for page in page_iterator:
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.lower().endswith((".jpg", ".jpeg", ".png")):
                    image_keys.append(key)

        logger.info(f"Found {len(image_keys)} image keys to check.")

        # Use ProcessPoolExecutor for CPU-bound face encoding/compare
        matched = []
        if image_keys:
            # prepare args list for workers, but we chunk so worker pickles are efficient
            worker_args = ((k, known_encoding, FACE_COMPARE_TOLERANCE) for k in image_keys)
            with concurrent.futures.ProcessPoolExecutor(max_workers=MAX_WORKERS) as exe:
                futures = {exe.submit(download_and_compare_worker, arg): arg for arg in worker_args}
                for fut in concurrent.futures.as_completed(futures):
                    try:
                        res = fut.result()
                        if res:
                            matched.append(res)
                            logger.info(f"Matched key: {res['key']}")
                            # stop if too many matches
                            if len(matched) >= MAX_MATCHES:
                                logger.info("Reached MAX_MATCHES limit, cancelling remaining tasks.")
                                # Cancel remaining futures
                                for f in futures:
                                    if not f.done():
                                        f.cancel()
                                break
                    except Exception as e:
                        logger.exception(f"Exception in worker future: {e}")

        logger.info(f"Total matches found: {len(matched)}")

        zip_url = None
        # If matches found, create ZIP of local matched files and upload to S3
        if matched:
            # create zip file in temp
            zip_fd, zip_path = tempfile.mkstemp(suffix=".zip")
            os.close(zip_fd)
            try:
                with ZipFile(zip_path, "w") as zipf:
                    for m in matched:
                        local_path = m.get("local_path")
                        if local_path and os.path.exists(local_path):
                            zipf.write(local_path, arcname=os.path.basename(local_path))
                            # remove the local file after adding
                            try:
                                os.unlink(local_path)
                            except:
                                pass

                # upload zip to S3 under temp_zips/
                temp_zip_key = f"temp_zips/{uuid.uuid4()}.zip"
                s3.upload_file(Filename=zip_path, Bucket=BUCKET, Key=temp_zip_key)

                # create presigned url
                zip_url = create_presigned_url(BUCKET, temp_zip_key, expiration=3600)

                # remove local zip
                try:
                    os.unlink(zip_path)
                except:
                    pass

                # send email with link
                send_link_email(zip_url, recipient_email, name, phone)
            except Exception as e:
                logger.exception(f"Error creating/uploading zip: {e}")
                try:
                    if os.path.exists(zip_path):
                        os.unlink(zip_path)
                except:
                    pass

        # optional: copy matched images to a user folder for browsing
        user_id = str(uuid.uuid4())
        user_folder = f"clients/{user_id}/"
        for m in matched:
            try:
                src = {"Bucket": BUCKET, "Key": m["key"]}
                dest_key = f"{user_folder}{os.path.basename(m['key'])}"
                s3.copy_object(Bucket=BUCKET, CopySource=src, Key=dest_key)
            except Exception:
                logger.exception(f"Failed to copy matched object {m['key']} to {user_folder}")

        share_url = f"https://{BUCKET}.s3.{REGION}.wasabisys.com/{user_folder}" if matched else ""

        response = {
            "success": True,
            "message": f"Matched {len(matched)} images. Download link sent to {recipient_email}." if matched else "No matches found.",
            "matched_count": len(matched),
            "zip_download_url": zip_url,
            "shared_url": share_url
        }
        logger.info("Matching operation completed.")
        return jsonify(response), 200, {'Access-Control-Allow-Origin': '*'}

    except Exception as e:
        logger.exception(f"Matching failed: {e}")
        return jsonify({"success": False, "message": str(e)}), 500, {'Access-Control-Allow-Origin': '*'}


if __name__ == "__main__":
    logger.info("Face matcher server starting...")
    # production should use gunicorn; this is for local debug only
    app.run(debug=True, host="0.0.0.0", port=5000)
