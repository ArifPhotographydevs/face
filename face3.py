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

# ===== FLASK APP =====
app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# ===== WASABI S3 CONFIG =====
ACCESS_KEY = "SW5I2XCNJAI7GTB7MRIW"
SECRET_KEY = "eKNEI3erAhnSiBdcK0OltkTHIe2jJYJVhPu1eazJ"
REGION = "ap-northeast-1"
BUCKET = "arif12"
ENDPOINT_URL = f"https://s3.{REGION}.wasabisys.com"
boto_config = Config(max_pool_connections=30)
s3 = boto3.client(
    "s3",
    aws_access_key_id=ACCESS_KEY,
    aws_secret_access_key=SECRET_KEY,
    region_name=REGION,
    endpoint_url=ENDPOINT_URL,
    config=boto_config
)

def safe_load_and_resize(image_path, max_size=(1600, 1600)):
    """Load and resize an image, converting to RGB if needed."""
    try:
        with Image.open(image_path) as img:
            if hasattr(Image, "Resampling"):
                resample = Image.Resampling.LANCZOS
            else:
                resample = getattr(Image, "LANCZOS", Image.BICUBIC)
            img.thumbnail(max_size, resample)
            if img.mode in ('RGBA', 'LA', 'P'):
                img = img.convert('RGB')
            img.save(image_path)
    except Exception as e:
        logger.warning(f"Image resize failed for {image_path}: {e}")
    return face_recognition.load_image_file(image_path)

def download_and_match(key, known_encoding):
    temp_path = None
    try:
        logger.info(f"Matching started for: {key}")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_file:
            temp_path = temp_file.name
        s3.download_file(Bucket=BUCKET, Key=key, Filename=temp_path)
        try:
            with Image.open(temp_path) as img:
                img.verify()
        except Exception as e:
            logger.warning(f"File skipped (cannot identify image file): {key} - {e}")
            os.unlink(temp_path)
            return None
        try:
            image = safe_load_and_resize(temp_path)
            encodings = face_recognition.face_encodings(image)
        except Exception as e:
            logger.error(f"Face encoding failed for {key}: {e}")
            os.unlink(temp_path)
            return None
        if encodings and any(face_recognition.compare_faces([known_encoding], encoding)[0] for encoding in encodings):
            with open(temp_path, "rb") as img_file:
                img_base64 = base64.b64encode(img_file.read()).decode('utf-8')
            logger.info(f"Matched: {key}")
            return {"key": key, "image": img_base64, "local_path": temp_path}
        logger.info(f"Not matched: {key}")
        return None
    except Exception:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
        return None

def create_presigned_url(bucket, key, expiration=3600):
    return s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket, 'Key': key},
        ExpiresIn=expiration
    )

def send_link_email(download_url, recipient_email, name, phone):
    # SMTP Gmail config
    SMTP_SERVER = "smtp.gmail.com"
    SMTP_PORT = 587
    SENDER_EMAIL = "githubarifphotography@gmail.com"
    SENDER_PASSWORD = "utuz rvgk kmsv sntz"  # App password or real password

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
        data = request.get_json()
        for field in ["selfie", "email", "name", "phone"]:
            if not data.get(field):
                return jsonify({"success": False, "message": f"Missing {field}"}), 400, {
                    'Access-Control-Allow-Origin': '*'
                }
        name = data["name"]
        recipient_email = data["email"]
        phone = data["phone"]
        selfie_data = base64.b64decode(data["selfie"])
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
            tmp.write(selfie_data)
            selfie_path = tmp.name
        try:
            try:
                with Image.open(selfie_path) as test_img:
                    test_img.verify()
            except (UnidentifiedImageError, Exception) as e:
                logger.warning(f"Uploaded selfie is not a valid image: {e}")
                os.unlink(selfie_path)
                return jsonify({"success": False, "message": "Uploaded selfie is not a valid image."}), 400, {
                    'Access-Control-Allow-Origin': '*'
                }
            known_image = safe_load_and_resize(selfie_path)
            known_encodings = face_recognition.face_encodings(known_image)
        finally:
            try:
                os.unlink(selfie_path)
            except Exception:
                pass
        if not known_encodings:
            return jsonify({"success": False, "message": "No face found in selfie"}), 400, {
                'Access-Control-Allow-Origin': '*'
            }
        known_encoding = known_encodings[0]
        all_objs = []
        continuation_token = None
        while True:
            if continuation_token:
                resp = s3.list_objects_v2(Bucket=BUCKET, ContinuationToken=continuation_token)
            else:
                resp = s3.list_objects_v2(Bucket=BUCKET)
            all_objs.extend([obj for obj in resp.get("Contents", []) if obj["Key"].lower().endswith((".jpg", ".jpeg", ".png"))])
            if not resp.get("IsTruncated"):
                break
            continuation_token = resp.get("NextContinuationToken")
        image_keys = [obj["Key"] for obj in all_objs]
        logger.info(f"Processing {len(image_keys)} images for face matching")
        matched_images = []
        matched_paths = []
        max_workers = 2
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(download_and_match, key, known_encoding) for key in image_keys]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result:
                    matched_images.append({
                        "key": result["key"],
                        "image": result["image"]
                    })
                    matched_paths.append(result["local_path"])

        # Create zip of matched images and upload to temp_zips
        zip_path = None
        zip_url = None
        if matched_paths:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as zip_tmp:
                zip_path = zip_tmp.name
            with ZipFile(zip_path, "w") as zipf:
                for p in matched_paths:
                    if os.path.exists(p):
                        zipf.write(p, arcname=os.path.basename(p))
            temp_zip_key = f"temp_zips/{uuid.uuid4()}.zip"
            s3.upload_file(zip_path, BUCKET, temp_zip_key)
            zip_url = create_presigned_url(BUCKET, temp_zip_key, expiration=3600)
            # Remove local temp files
            for p in matched_paths:
                try:
                    os.unlink(p)
                except Exception:
                    pass
            os.unlink(zip_path)
            send_link_email(zip_url, recipient_email, name, phone)

        # Normal S3 copy for user browsing (unchanged, optional)
        user_id = str(uuid.uuid4())
        user_folder = f"clients/{user_id}/"
        for img in matched_images:
            dest_key = f"{user_folder}{os.path.basename(img['key'])}"
            s3.copy_object(Bucket=BUCKET, CopySource={"Bucket": BUCKET, "Key": img["key"]}, Key=dest_key)
        share_url = f"https://{BUCKET}.s3.{REGION}.wasabisys.com/{user_folder}" if matched_images else ""
        response = {
            "success": True,
            "message": f"Matched {len(matched_images)} images. Download link sent to {recipient_email}.",
            "matched_images": matched_images,
            "shared_url": share_url,
            "zip_download_url": zip_url
        }
        logger.info(f"Matching complete. {len(matched_images)} matches found. Link emailed to {recipient_email}")
        return jsonify(response), 200, {
            'Access-Control-Allow-Origin': '*'
        }
    except Exception as e:
        logger.error(f"Matching failed: {e}")
        return jsonify({"success": False, "message": str(e)}), 500, {
            'Access-Control-Allow-Origin': '*'
        }

if __name__ == "__main__":
    logger.info("Face matcher server starting...")
    app.run(debug=True, host="0.0.0.0", port=5000)
