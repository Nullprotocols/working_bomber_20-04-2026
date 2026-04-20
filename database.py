# database.py
import motor.motor_asyncio
from config import MONGO_URI, DATABASE_NAME
from typing import List, Dict, Optional
from datetime import datetime

# ------------------------------------------------------------------
# MongoDB क्लाइंट और कलेक्शंस
# ------------------------------------------------------------------
client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = client[DATABASE_NAME]

users_col = db.users
protected_col = db.protected_numbers
settings_col = db.settings

# ------------------------------------------------------------------
# यूजर ऑपरेशंस
# ------------------------------------------------------------------
async def add_user(user_id: int, username: str = None, first_name: str = None):
    """नया यूजर जोड़ें या मौजूदा को अपडेट करें।"""
    await users_col.update_one(
        {"user_id": user_id},
        {
            "$setOnInsert": {
                "username": username,
                "first_name": first_name,
                "role": "user",
                "joined_at": datetime.utcnow(),
                "banned": False,
                "target_number": None,
                "user_phone": None
            }
        },
        upsert=True
    )

async def is_admin(user_id: int) -> bool:
    """जांचें कि यूजर एडमिन या ओनर है या नहीं।"""
    from config import OWNER_ID
    if user_id == OWNER_ID:
        return True
    user = await users_col.find_one({"user_id": user_id})
    return user is not None and user.get("role") == "admin"

async def is_owner(user_id: int) -> bool:
    """जांचें कि यूजर बॉट का ओनर है या नहीं।"""
    from config import OWNER_ID
    return user_id == OWNER_ID

async def set_admin_role(user_id: int, make_admin: bool):
    """यूजर को एडमिन बनाएं या एडमिन से हटाएं।"""
    role = "admin" if make_admin else "user"
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"role": role}}
    )

async def ban_user(user_id: int) -> bool:
    """यूजर को बैन करें। सफल होने पर True लौटाएं।"""
    result = await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"banned": True}}
    )
    return result.modified_count > 0

async def unban_user(user_id: int) -> bool:
    """यूजर को अनबैन करें। सफल होने पर True लौटाएं।"""
    result = await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"banned": False}}
    )
    return result.modified_count > 0

async def delete_user(user_id: int) -> bool:
    """यूजर को डेटाबेस से हमेशा के लिए हटाएं।"""
    result = await users_col.delete_one({"user_id": user_id})
    return result.deleted_count > 0

async def get_user_by_id(user_id: int) -> Optional[Dict]:
    """यूजर की पूरी जानकारी प्राप्त करें।"""
    return await users_col.find_one({"user_id": user_id})

async def update_user_target(user_id: int, target: str):
    """यूजर द्वारा बॉम्ब किए जा रहे टारगेट नंबर को सेव करें।"""
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"target_number": target}}
    )

async def get_user_target(user_id: int) -> Optional[str]:
    """यूजर का सेव किया हुआ टारगेट नंबर प्राप्त करें।"""
    user = await users_col.find_one({"user_id": user_id})
    return user.get("target_number") if user else None

async def update_user_phone(user_id: int, phone: str):
    """यूजर का अपना फ़ोन नंबर सेव करें (सेल्फ-बॉम्बिंग रोकने के लिए)।"""
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"user_phone": phone}}
    )

async def get_user_phone(user_id: int) -> Optional[str]:
    """यूजर का सेव किया हुआ अपना फ़ोन नंबर प्राप्त करें।"""
    user = await users_col.find_one({"user_id": user_id})
    return user.get("user_phone") if user else None

async def get_all_users_paginated(page: int, per_page: int = 10) -> List[Dict]:
    """पेजिनेशन के साथ सभी यूजर्स की लिस्ट प्राप्त करें।"""
    skip = page * per_page
    cursor = users_col.find().sort("user_id", 1).skip(skip).limit(per_page)
    return await cursor.to_list(length=per_page)

async def get_all_user_ids() -> List[int]:
    """सभी यूजर्स के आईडी की लिस्ट प्राप्त करें (ब्रॉडकास्ट के लिए)।"""
    cursor = users_col.find({}, {"user_id": 1})
    users = await cursor.to_list(length=None)
    return [u["user_id"] for u in users]

# ------------------------------------------------------------------
# प्रोटेक्टेड नंबर्स
# ------------------------------------------------------------------
async def add_protected_number(number: str, added_by: int) -> bool:
    """नया नंबर प्रोटेक्टेड लिस्ट में जोड़ें।"""
    existing = await protected_col.find_one({"number": number})
    if existing:
        return False
    await protected_col.insert_one({
        "number": number,
        "added_by": added_by,
        "added_at": datetime.utcnow()
    })
    return True

async def remove_protected_number(number: str) -> bool:
    """नंबर को प्रोटेक्टेड लिस्ट से हटाएं।"""
    result = await protected_col.delete_one({"number": number})
    return result.deleted_count > 0

async def is_protected(number: str) -> bool:
    """जांचें कि नंबर प्रोटेक्टेड है या नहीं।"""
    return await protected_col.find_one({"number": number}) is not None

async def get_all_protected_numbers() -> List[str]:
    """सभी प्रोटेक्टेड नंबरों की लिस्ट प्राप्त करें।"""
    cursor = protected_col.find({}, {"number": 1}).sort("added_at", -1)
    docs = await cursor.to_list(length=None)
    return [doc["number"] for doc in docs]

# ------------------------------------------------------------------
# सेटिंग्स (डायनमिक इंटरवल)
# ------------------------------------------------------------------
async def get_settings() -> Dict:
    """बॉम्बिंग इंटरवल की सेटिंग्स प्राप्त करें (न होने पर डिफ़ॉल्ट बनाएं)।"""
    settings = await settings_col.find_one({"_id": "bombing_intervals"})
    if not settings:
        from config import DEFAULT_CALL_INTERVAL, DEFAULT_SMS_INTERVAL
        settings = {
            "_id": "bombing_intervals",
            "call_interval": DEFAULT_CALL_INTERVAL,
            "sms_interval": DEFAULT_SMS_INTERVAL
        }
        await settings_col.insert_one(settings)
    return settings

async def update_call_interval(seconds: int):
    """कॉल API का इंटरवल अपडेट करें।"""
    await settings_col.update_one(
        {"_id": "bombing_intervals"},
        {"$set": {"call_interval": seconds}},
        upsert=True
    )

async def update_sms_interval(seconds: int):
    """SMS/WhatsApp API का इंटरवल अपडेट करें।"""
    await settings_col.update_one(
        {"_id": "bombing_intervals"},
        {"$set": {"sms_interval": seconds}},
        upsert=True
    )
