from fastapi import FastAPI, APIRouter, HTTPException, Request, Header, Response, Depends
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional, Dict
import uuid
from datetime import datetime, timezone, timedelta
import httpx
from emergentintegrations.payments.stripe.checkout import StripeCheckout, CheckoutSessionResponse, CheckoutStatusResponse, CheckoutSessionRequest

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Create the main app without a prefix
app = FastAPI()

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==================== MODELS ====================

class User(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    id: str = Field(alias="_id")
    email: str
    name: str
    picture: str = ""
    role: str = "customer"  # customer, admin, tailor
    phone: Optional[str] = None
    address: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class UserSession(BaseModel):
    user_id: str
    session_token: str
    expires_at: datetime
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class BodyMeasurement(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    # Standard measurements (in cm)
    bust: Optional[float] = None
    waist: Optional[float] = None
    hip: Optional[float] = None
    height: Optional[float] = None
    shoulder_width: Optional[float] = None
    sleeve_length: Optional[float] = None
    inseam: Optional[float] = None
    neck: Optional[float] = None
    arm_length: Optional[float] = None
    wrist: Optional[float] = None
    # Custom measurements
    custom_measurements: Optional[Dict[str, float]] = {}
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class ReadyToWear(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str
    price: float
    category: str  # dress, top, skirt, pants, etc.
    sizes: List[str] = ["XS", "S", "M", "L", "XL"]
    colors: List[str] = []
    images: List[str] = []
    stock: int = 0
    featured: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class CustomOrder(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    title: str
    description: str
    reference_images: List[str] = []
    status: str = "pending"  # pending, measuring, cutting, stitching, finishing, ready, delivered
    estimated_delivery: Optional[datetime] = None
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class ProgressUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    order_id: str
    status: str
    message: str
    images: List[str] = []
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class TaskAssignment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    order_id: str
    tailor_id: str
    task_description: str
    status: str = "assigned"  # assigned, in_progress, completed
    due_date: Optional[datetime] = None
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class CartItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    product_id: str
    size: str
    color: str
    quantity: int = 1
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class Order(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    items: List[Dict] = []  # [{product_id, name, size, color, quantity, price}]
    total_amount: float
    payment_status: str = "pending"  # pending, paid, failed
    payment_session_id: Optional[str] = None
    shipping_address: str
    order_status: str = "processing"  # processing, shipped, delivered
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class PaymentTransaction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    user_id: Optional[str] = None
    order_id: Optional[str] = None
    amount: float
    currency: str = "usd"
    payment_status: str = "pending"  # pending, paid, failed, expired
    metadata: Optional[Dict[str, str]] = {}
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

# ==================== AUTH HELPER ====================

async def get_current_user(request: Request) -> Optional[User]:
    # Check cookie first
    session_token = request.cookies.get("session_token")
    
    # Fallback to Authorization header
    if not session_token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            session_token = auth_header.replace("Bearer ", "")
    
    if not session_token:
        return None
    
    # Check session in database
    session = await db.user_sessions.find_one({
        "session_token": session_token,
        "expires_at": {"$gt": datetime.now(timezone.utc)}
    })
    
    if not session:
        return None
    
    # Get user
    user_doc = await db.users.find_one({"_id": session["user_id"]})
    if not user_doc:
        return None
    
    return User(**user_doc)

async def require_auth(request: Request) -> User:
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user

async def require_admin(request: Request) -> User:
    user = await require_auth(request)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user

# ==================== AUTH ROUTES ====================

@api_router.get("/auth/me")
async def get_me(request: Request):
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user

@api_router.post("/auth/session")
async def create_session(request: Request, response: Response, x_session_id: str = Header(..., alias="X-Session-ID")):
    # Call Emergent auth service to get session data
    async with httpx.AsyncClient() as client:
        try:
            auth_response = await client.get(
                "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data",
                headers={"X-Session-ID": x_session_id}
            )
            auth_response.raise_for_status()
            session_data = auth_response.json()
        except Exception as e:
            logger.error(f"Error getting session data: {e}")
            raise HTTPException(status_code=401, detail="Invalid session")
    
    # Check if user exists
    user_doc = await db.users.find_one({"_id": session_data["id"]})
    
    if not user_doc:
        # Create new user
        new_user = User(
            id=session_data["id"],
            email=session_data["email"],
            name=session_data["name"],
            picture=session_data.get("picture", ""),
            role="customer"
        )
        user_dict = new_user.model_dump(by_alias=True)
        user_dict["created_at"] = user_dict["created_at"].isoformat()
        await db.users.insert_one(user_dict)
        user = new_user
    else:
        user = User(**user_doc)
    
    # Create session in database
    session_token = session_data["session_token"]
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    
    session = UserSession(
        user_id=user.id,
        session_token=session_token,
        expires_at=expires_at
    )
    
    session_dict = session.model_dump()
    session_dict["created_at"] = session_dict["created_at"].isoformat()
    session_dict["expires_at"] = session_dict["expires_at"].isoformat()
    
    await db.user_sessions.insert_one(session_dict)
    
    # Set cookie
    response.set_cookie(
        key="session_token",
        value=session_token,
        httponly=True,
        secure=True,
        samesite="none",
        path="/",
        max_age=7 * 24 * 60 * 60
    )
    
    return {"user": user, "session_token": session_token}

@api_router.post("/auth/logout")
async def logout(request: Request, response: Response):
    user = await get_current_user(request)
    if user:
        session_token = request.cookies.get("session_token")
        if session_token:
            await db.user_sessions.delete_one({"session_token": session_token})
    
    response.delete_cookie("session_token", path="/")
    return {"message": "Logged out successfully"}

# ==================== MEASUREMENTS ====================

@api_router.post("/measurements", response_model=BodyMeasurement)
async def create_or_update_measurements(measurement: BodyMeasurement, request: Request):
    user = await require_auth(request)
    measurement.user_id = user.id
    measurement.updated_at = datetime.now(timezone.utc)
    
    # Check if measurements exist
    existing = await db.body_measurements.find_one({"user_id": user.id})
    
    doc = measurement.model_dump()
    doc["created_at"] = doc["created_at"].isoformat()
    doc["updated_at"] = doc["updated_at"].isoformat()
    
    if existing:
        await db.body_measurements.update_one({"user_id": user.id}, {"$set": doc})
    else:
        await db.body_measurements.insert_one(doc)
    
    return measurement

@api_router.get("/measurements")
async def get_my_measurements(request: Request):
    user = await require_auth(request)
    measurement = await db.body_measurements.find_one({"user_id": user.id}, {"_id": 0})
    if not measurement:
        return None
    return measurement

# ==================== READY-TO-WEAR ====================

@api_router.get("/products", response_model=List[ReadyToWear])
async def get_products(featured: Optional[bool] = None, category: Optional[str] = None):
    query = {}
    if featured is not None:
        query["featured"] = featured
    if category:
        query["category"] = category
    
    products = await db.ready_to_wear.find(query, {"_id": 0}).to_list(1000)
    return products

@api_router.get("/products/{product_id}")
async def get_product(product_id: str):
    product = await db.ready_to_wear.find_one({"id": product_id}, {"_id": 0})
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product

@api_router.post("/products", response_model=ReadyToWear)
async def create_product(product: ReadyToWear, request: Request):
    await require_admin(request)
    
    doc = product.model_dump()
    doc["created_at"] = doc["created_at"].isoformat()
    doc["updated_at"] = doc["updated_at"].isoformat()
    
    await db.ready_to_wear.insert_one(doc)
    return product

@api_router.put("/products/{product_id}", response_model=ReadyToWear)
async def update_product(product_id: str, product: ReadyToWear, request: Request):
    await require_admin(request)
    
    product.updated_at = datetime.now(timezone.utc)
    doc = product.model_dump()
    doc["updated_at"] = doc["updated_at"].isoformat()
    
    await db.ready_to_wear.update_one({"id": product_id}, {"$set": doc})
    return product

@api_router.delete("/products/{product_id}")
async def delete_product(product_id: str, request: Request):
    await require_admin(request)
    await db.ready_to_wear.delete_one({"id": product_id})
    return {"message": "Product deleted"}

# ==================== CART ====================

@api_router.post("/cart")
async def add_to_cart(item: CartItem, request: Request):
    user = await require_auth(request)
    item.user_id = user.id
    
    # Check if item already exists in cart
    existing = await db.cart_items.find_one({
        "user_id": user.id,
        "product_id": item.product_id,
        "size": item.size,
        "color": item.color
    })
    
    if existing:
        # Update quantity
        await db.cart_items.update_one(
            {"id": existing["id"]},
            {"$inc": {"quantity": item.quantity}}
        )
        return {"message": "Cart updated"}
    
    doc = item.model_dump()
    doc["created_at"] = doc["created_at"].isoformat()
    await db.cart_items.insert_one(doc)
    return {"message": "Item added to cart"}

@api_router.get("/cart")
async def get_cart(request: Request):
    user = await require_auth(request)
    items = await db.cart_items.find({"user_id": user.id}, {"_id": 0}).to_list(1000)
    
    # Populate product details
    for item in items:
        product = await db.ready_to_wear.find_one({"id": item["product_id"]}, {"_id": 0})
        if product:
            item["product"] = product
    
    return items

@api_router.delete("/cart/{item_id}")
async def remove_from_cart(item_id: str, request: Request):
    user = await require_auth(request)
    await db.cart_items.delete_one({"id": item_id, "user_id": user.id})
    return {"message": "Item removed from cart"}

@api_router.delete("/cart")
async def clear_cart(request: Request):
    user = await require_auth(request)
    await db.cart_items.delete_many({"user_id": user.id})
    return {"message": "Cart cleared"}

# ==================== ORDERS ====================

@api_router.post("/orders")
async def create_order(order: Order, request: Request):
    user = await require_auth(request)
    order.user_id = user.id
    
    doc = order.model_dump()
    doc["created_at"] = doc["created_at"].isoformat()
    doc["updated_at"] = doc["updated_at"].isoformat()
    
    await db.orders.insert_one(doc)
    return order

@api_router.get("/orders")
async def get_my_orders(request: Request):
    user = await require_auth(request)
    orders = await db.orders.find({"user_id": user.id}, {"_id": 0}).sort("created_at", -1).to_list(1000)
    return orders

@api_router.get("/orders/{order_id}")
async def get_order(order_id: str, request: Request):
    user = await require_auth(request)
    order = await db.orders.find_one({"id": order_id, "user_id": user.id}, {"_id": 0})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order

# ==================== CUSTOM ORDERS ====================

@api_router.post("/custom-orders", response_model=CustomOrder)
async def create_custom_order(order: CustomOrder, request: Request):
    user = await require_auth(request)
    order.user_id = user.id
    
    doc = order.model_dump()
    doc["created_at"] = doc["created_at"].isoformat()
    doc["updated_at"] = doc["updated_at"].isoformat()
    
    await db.custom_orders.insert_one(doc)
    return order

@api_router.get("/custom-orders")
async def get_my_custom_orders(request: Request):
    user = await require_auth(request)
    orders = await db.custom_orders.find({"user_id": user.id}, {"_id": 0}).sort("created_at", -1).to_list(1000)
    return orders

@api_router.get("/custom-orders/{order_id}")
async def get_custom_order(order_id: str, request: Request):
    user = await require_auth(request)
    order = await db.custom_orders.find_one({"id": order_id, "user_id": user.id}, {"_id": 0})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order

@api_router.get("/custom-orders/{order_id}/progress")
async def get_order_progress(order_id: str, request: Request):
    user = await require_auth(request)
    
    # Verify order belongs to user
    order = await db.custom_orders.find_one({"id": order_id, "user_id": user.id})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    updates = await db.progress_updates.find({"order_id": order_id}, {"_id": 0}).sort("created_at", -1).to_list(1000)
    return updates

# ==================== ADMIN ROUTES ====================

@api_router.get("/admin/custom-orders")
async def admin_get_custom_orders(request: Request):
    await require_admin(request)
    orders = await db.custom_orders.find({}, {"_id": 0}).sort("created_at", -1).to_list(1000)
    
    # Populate user details
    for order in orders:
        user = await db.users.find_one({"_id": order["user_id"]}, {"_id": 0})
        if user:
            order["user"] = user
    
    return orders

@api_router.put("/admin/custom-orders/{order_id}")
async def admin_update_custom_order(order_id: str, order: CustomOrder, request: Request):
    await require_admin(request)
    
    order.updated_at = datetime.now(timezone.utc)
    doc = order.model_dump()
    doc["updated_at"] = doc["updated_at"].isoformat()
    
    await db.custom_orders.update_one({"id": order_id}, {"$set": doc})
    return order

@api_router.post("/admin/custom-orders/{order_id}/progress")
async def admin_add_progress_update(order_id: str, update: ProgressUpdate, request: Request):
    await require_admin(request)
    update.order_id = order_id
    
    # Also update order status
    await db.custom_orders.update_one(
        {"id": order_id},
        {"$set": {"status": update.status, "updated_at": datetime.now(timezone.utc).isoformat()}}
    )
    
    doc = update.model_dump()
    doc["created_at"] = doc["created_at"].isoformat()
    await db.progress_updates.insert_one(doc)
    return update

@api_router.get("/admin/customers")
async def admin_get_customers(request: Request):
    await require_admin(request)
    customers = await db.users.find({"role": "customer"}, {"_id": 0}).to_list(1000)
    
    # Populate measurements
    for customer in customers:
        measurements = await db.body_measurements.find_one({"user_id": customer["id"]}, {"_id": 0})
        customer["measurements"] = measurements
    
    return customers

@api_router.get("/admin/tailors")
async def admin_get_tailors(request: Request):
    await require_admin(request)
    tailors = await db.users.find({"role": "tailor"}, {"_id": 0}).to_list(1000)
    return tailors

@api_router.post("/admin/tailors")
async def admin_create_tailor(user: User, request: Request):
    await require_admin(request)
    user.role = "tailor"
    
    doc = user.model_dump(by_alias=True)
    doc["created_at"] = doc["created_at"].isoformat()
    await db.users.insert_one(doc)
    return user

@api_router.post("/admin/tasks")
async def admin_create_task(task: TaskAssignment, request: Request):
    await require_admin(request)
    
    doc = task.model_dump()
    doc["created_at"] = doc["created_at"].isoformat()
    doc["updated_at"] = doc["updated_at"].isoformat()
    
    await db.task_assignments.insert_one(doc)
    return task

@api_router.get("/admin/tasks")
async def admin_get_tasks(request: Request):
    await require_admin(request)
    tasks = await db.task_assignments.find({}, {"_id": 0}).sort("created_at", -1).to_list(1000)
    
    # Populate tailor and order details
    for task in tasks:
        tailor = await db.users.find_one({"_id": task["tailor_id"]}, {"_id": 0})
        order = await db.custom_orders.find_one({"id": task["order_id"]}, {"_id": 0})
        if tailor:
            task["tailor"] = tailor
        if order:
            task["order"] = order
    
    return tasks

@api_router.get("/admin/dashboard")
async def admin_dashboard(request: Request):
    await require_admin(request)
    
    total_customers = await db.users.count_documents({"role": "customer"})
    total_custom_orders = await db.custom_orders.count_documents({})
    pending_orders = await db.custom_orders.count_documents({"status": "pending"})
    total_products = await db.ready_to_wear.count_documents({})
    
    return {
        "total_customers": total_customers,
        "total_custom_orders": total_custom_orders,
        "pending_orders": pending_orders,
        "total_products": total_products
    }

# ==================== TAILOR ROUTES ====================

@api_router.get("/tailor/tasks")
async def tailor_get_tasks(request: Request):
    user = await require_auth(request)
    if user.role != "tailor":
        raise HTTPException(status_code=403, detail="Tailor access required")
    
    tasks = await db.task_assignments.find({"tailor_id": user.id}, {"_id": 0}).sort("created_at", -1).to_list(1000)
    
    # Populate order details
    for task in tasks:
        order = await db.custom_orders.find_one({"id": task["order_id"]}, {"_id": 0})
        if order:
            task["order"] = order
    
    return tasks

@api_router.put("/tailor/tasks/{task_id}")
async def tailor_update_task(task_id: str, task: TaskAssignment, request: Request):
    user = await require_auth(request)
    if user.role != "tailor":
        raise HTTPException(status_code=403, detail="Tailor access required")
    
    # Verify task belongs to tailor
    existing = await db.task_assignments.find_one({"id": task_id, "tailor_id": user.id})
    if not existing:
        raise HTTPException(status_code=404, detail="Task not found")
    
    task.updated_at = datetime.now(timezone.utc)
    doc = task.model_dump()
    doc["updated_at"] = doc["updated_at"].isoformat()
    
    await db.task_assignments.update_one({"id": task_id}, {"$set": doc})
    return task

# ==================== PAYMENT ROUTES ====================

@api_router.post("/checkout/session")
async def create_checkout_session(request: Request):
    user = await require_auth(request)
    data = await request.json()
    
    # Get cart items
    cart_items = await db.cart_items.find({"user_id": user.id}).to_list(1000)
    if not cart_items:
        raise HTTPException(status_code=400, detail="Cart is empty")
    
    # Calculate total
    total = 0.0
    order_items = []
    
    for item in cart_items:
        product = await db.ready_to_wear.find_one({"id": item["product_id"]})
        if product:
            item_total = product["price"] * item["quantity"]
            total += item_total
            order_items.append({
                "product_id": product["id"],
                "name": product["name"],
                "size": item["size"],
                "color": item["color"],
                "quantity": item["quantity"],
                "price": product["price"]
            })
    
    # Create order
    order = Order(
        user_id=user.id,
        items=order_items,
        total_amount=total,
        shipping_address=data.get("shipping_address", "")
    )
    
    order_doc = order.model_dump()
    order_doc["created_at"] = order_doc["created_at"].isoformat()
    order_doc["updated_at"] = order_doc["updated_at"].isoformat()
    await db.orders.insert_one(order_doc)
    
    # Create Stripe checkout session
    origin_url = data.get("origin_url", "")
    success_url = f"{origin_url}/checkout/success?session_id={{{{CHECKOUT_SESSION_ID}}}}"
    cancel_url = f"{origin_url}/cart"
    
    stripe_checkout = StripeCheckout(
        api_key=os.environ["STRIPE_API_KEY"],
        webhook_url=f"{origin_url}/api/webhook/stripe"
    )
    
    checkout_request = CheckoutSessionRequest(
        amount=total,
        currency="usd",
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={
            "user_id": user.id,
            "order_id": order.id
        }
    )
    
    session = await stripe_checkout.create_checkout_session(checkout_request)
    
    # Update order with session ID
    await db.orders.update_one(
        {"id": order.id},
        {"$set": {"payment_session_id": session.session_id}}
    )
    
    # Create payment transaction
    transaction = PaymentTransaction(
        session_id=session.session_id,
        user_id=user.id,
        order_id=order.id,
        amount=total,
        currency="usd",
        metadata={"order_id": order.id}
    )
    
    trans_doc = transaction.model_dump()
    trans_doc["created_at"] = trans_doc["created_at"].isoformat()
    trans_doc["updated_at"] = trans_doc["updated_at"].isoformat()
    await db.payment_transactions.insert_one(trans_doc)
    
    return {"url": session.url, "session_id": session.session_id}

@api_router.get("/checkout/status/{session_id}")
async def check_payment_status(session_id: str, request: Request):
    user = await require_auth(request)
    
    # Check transaction
    transaction = await db.payment_transactions.find_one({"session_id": session_id})
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")
    
    # If already paid, return immediately
    if transaction["payment_status"] == "paid":
        return {"status": "complete", "payment_status": "paid"}
    
    # Get status from Stripe
    stripe_checkout = StripeCheckout(
        api_key=os.environ["STRIPE_API_KEY"],
        webhook_url=""
    )
    
    status = await stripe_checkout.get_checkout_status(session_id)
    
    # Update transaction and order
    if status.payment_status == "paid" and transaction["payment_status"] != "paid":
        await db.payment_transactions.update_one(
            {"session_id": session_id},
            {"$set": {"payment_status": "paid", "updated_at": datetime.now(timezone.utc).isoformat()}}
        )
        
        # Update order
        order_id = transaction.get("order_id")
        if order_id:
            await db.orders.update_one(
                {"id": order_id},
                {"$set": {"payment_status": "paid", "updated_at": datetime.now(timezone.utc).isoformat()}}
            )
            
            # Clear cart
            await db.cart_items.delete_many({"user_id": user.id})
    
    return {"status": status.status, "payment_status": status.payment_status}

@api_router.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("Stripe-Signature")
    
    stripe_checkout = StripeCheckout(
        api_key=os.environ["STRIPE_API_KEY"],
        webhook_url=""
    )
    
    try:
        webhook_response = await stripe_checkout.handle_webhook(body, signature)
        
        # Update transaction
        await db.payment_transactions.update_one(
            {"session_id": webhook_response.session_id},
            {"$set": {"payment_status": webhook_response.payment_status, "updated_at": datetime.now(timezone.utc).isoformat()}}
        )
        
        # Update order if paid
        if webhook_response.payment_status == "paid":
            order_id = webhook_response.metadata.get("order_id")
            if order_id:
                await db.orders.update_one(
                    {"id": order_id},
                    {"$set": {"payment_status": "paid", "updated_at": datetime.now(timezone.utc).isoformat()}}
                )
        
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

# ==================== ROOT ====================

@api_router.get("/")
async def root():
    return {"message": "Fashion House API"}

# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
