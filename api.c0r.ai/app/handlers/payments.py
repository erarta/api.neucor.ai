"""
Telegram Payments handler for c0r.ai bot
Handles native Telegram payments without leaving the app
"""
import os
from aiogram import types
from loguru import logger
from common.supabase_client import get_or_create_user, add_credits, add_payment, log_user_action
from .keyboards import create_main_menu_keyboard, create_payment_success_keyboard
from config import PAYMENT_PLANS
import traceback
import json

# Environment variables
YOOKASSA_PROVIDER_TOKEN = os.getenv("YOOKASSA_PROVIDER_TOKEN")

# Payment plans configuration - now imported from config
# PAYMENT_PLANS imported from config.py

async def create_invoice_message(message: types.Message, plan_id: str = "basic", user_id: int = None):
    """
    Create and send Telegram invoice message
    """
    try:
        # Use passed user_id or fallback to message.from_user.id
        if user_id is None:
            user_id = message.from_user.id
        
        username = message.from_user.username
        logger.info(f"=== CREATE_INVOICE_MESSAGE DEBUG ===")
        logger.info(f"User ID: {user_id}")
        logger.info(f"Username: {username}")  
        logger.info(f"Message object type: {type(message)}")
        logger.info(f"Message from_user: {message.from_user}")
        logger.info(f"Plan: {plan_id}")
        logger.info(f"=====================================")
        
        if not YOOKASSA_PROVIDER_TOKEN:
            logger.error("YOOKASSA_PROVIDER_TOKEN is not set!")
            await message.answer("❌ Payment system is not configured. Please contact support.", reply_markup=create_main_menu_keyboard())
            return

        plan = PAYMENT_PLANS.get(plan_id)
        if not plan:
            logger.error(f"Invalid payment plan selected: {plan_id}")
            await message.answer("❌ Invalid payment plan selected.", reply_markup=create_main_menu_keyboard())
            return

        logger.info(f"[Telegram] Creating invoice: title={plan['title']}, price={plan['price']}, user_id={user_id}, provider_token={YOOKASSA_PROVIDER_TOKEN[:12]}..., payload=credits_{plan_id}_{user_id}")

        # Prepare provider_data for YooKassa receipt
        provider_data = json.dumps({
            "receipt": {
                "items": [
                    {
                        "description": plan["description"],
                        "quantity": 1,
                        "amount": {
                            "value": plan["price"] // 100,  # RUB, not kopecks
                            "currency": "RUB"
                        },
                        "vat_code": 1,
                        "payment_mode": "full_payment",
                        "payment_subject": "commodity"
                    }
                ],
                "tax_system_code": 1
            }
        })

        # Create invoice
        await message.answer_invoice(
            title=plan["title"],
            description=plan["description"],
            payload=f"credits_{plan_id}_{user_id}",
            provider_token=YOOKASSA_PROVIDER_TOKEN,
            currency=plan["currency"],
            prices=[
                types.LabeledPrice(
                    label=f"{plan['credits']} credits",
                    amount=plan["price"]
                )
            ],
            start_parameter=f"buy_{plan_id}",
            photo_url="https://api.c0r.ai/assets/logo_v2.png",
            photo_width=512,
            photo_height=512,
            need_email=True,
            send_email_to_provider=True,
            provider_data=provider_data,
            need_phone_number=False,
            need_shipping_address=False,
            is_flexible=False
        )
        
        logger.info(f"Created invoice for user {user_id}, plan {plan_id}")
        
    except Exception as e:
        logger.error(f"[Telegram] Failed to create invoice: {e!r}")
        logger.error(traceback.format_exc())
        await message.answer("❌ Failed to create payment. Please try again later.", reply_markup=create_main_menu_keyboard())

async def handle_buy_callback(callback: types.CallbackQuery):
    """
    Handle callback from buy buttons
    """
    try:
        telegram_user_id = callback.from_user.id
        logger.info(f"Buy callback received from user {telegram_user_id} (@{callback.from_user.username})")
        logger.info(f"Callback data: {callback.data}")
        
        # Extract plan_id from callback data
        plan_id = callback.data.split("_")[1]  # "buy_basic" -> "basic"
        logger.info(f"Plan selected by user {telegram_user_id}: {plan_id}")
        
        # Answer callback to remove loading state
        await callback.answer()
        
        # Create invoice message with explicit user_id
        logger.info(f"Creating invoice for user {telegram_user_id}, plan {plan_id}")
        await create_invoice_message(callback.message, plan_id, user_id=telegram_user_id)
        
    except Exception as e:
        logger.error(f"Failed to handle buy callback: {e}")
        await callback.answer("❌ Failed to create payment. Please try again later.")

async def handle_pre_checkout_query(pre_checkout_query: types.PreCheckoutQuery):
    """
    Handle pre-checkout query - validate payment before processing
    """
    try:
        # Parse payload to get plan and user info
        payload = pre_checkout_query.invoice_payload
        if not payload.startswith("credits_"):
            await pre_checkout_query.answer(ok=False, error_message="Invalid payment payload")
            return

        # Extract plan_id and user_id from payload
        parts = payload.split("_")
        if len(parts) != 3:
            await pre_checkout_query.answer(ok=False, error_message="Invalid payment format")
            return

        plan_id = parts[1]
        user_id = int(parts[2])
        
        # Validate plan exists
        plan = PAYMENT_PLANS.get(plan_id)
        if not plan:
            await pre_checkout_query.answer(ok=False, error_message="Invalid payment plan")
            return

        # Validate user exists
        user = await get_or_create_user(user_id)
        if not user:
            await pre_checkout_query.answer(ok=False, error_message="User not found")
            return

        # Validate amount
        if pre_checkout_query.total_amount != plan["price"]:
            await pre_checkout_query.answer(ok=False, error_message="Invalid payment amount")
            return

        # All validations passed
        await pre_checkout_query.answer(ok=True)
        logger.info(f"Pre-checkout approved for user {user_id}, plan {plan_id}")
        
    except Exception as e:
        logger.error(f"Pre-checkout validation failed: {e}")
        await pre_checkout_query.answer(ok=False, error_message="Payment validation failed")

async def handle_successful_payment(message: types.Message):
    """
    Handle successful payment - add credits to user account
    """
    try:
        payment = message.successful_payment
        payload = payment.invoice_payload
        telegram_user_id = message.from_user.id
        
        logger.info(f"Processing successful payment for user {telegram_user_id}")
        logger.info(f"Payment data: {payment}")
        logger.info(f"Payment payload: {payload}")
        
        # Parse payload to get plan and user info
        if not payload.startswith("credits_"):
            logger.error(f"Invalid payment payload for user {telegram_user_id}: {payload}")
            return

        parts = payload.split("_")
        if len(parts) != 3:
            logger.error(f"Invalid payment format for user {telegram_user_id}: {payload}")
            return

        plan_id = parts[1]
        user_id = int(parts[2])
        
        logger.info(f"Payment details - plan: {plan_id}, user_id: {user_id}, telegram_user_id: {telegram_user_id}")
        
        # Get plan details
        plan = PAYMENT_PLANS.get(plan_id)
        if not plan:
            logger.error(f"Invalid plan in payment for user {telegram_user_id}: {plan_id}")
            return

        logger.info(f"Plan details: {plan}")
        
        # Get user before adding credits
        user_before = await get_or_create_user(user_id)
        logger.info(f"User before payment: {user_before}")
        
        # Add credits to user account
        updated_user = await add_credits(user_id, plan["credits"])
        logger.info(f"User after payment: {updated_user}")
        
        # Add payment record to database
        payment_amount = payment.total_amount / 100  # Convert kopecks to rubles
        await add_payment(
            user_id=updated_user['id'],
            amount=payment_amount,
            gateway="telegram_payments",
            status="succeeded"
        )
        
        # Log payment action
        await log_user_action(
            user_id=updated_user['id'],
            action_type="payment_success",
            metadata={
                "plan_id": plan_id,
                "credits_added": plan["credits"],
                "amount_paid": payment_amount,
                "currency": payment.currency,
                "payment_charge_id": payment.telegram_payment_charge_id,
                "provider_payment_charge_id": payment.provider_payment_charge_id
            }
        )
        
        # Send confirmation message
        await message.answer(
            f"✅ **Payment Successful!**\n\n"
            f"💳 **Plan**: {plan['title']}\n"
            f"⚡ **Credits Added**: {plan['credits']}\n"
            f"💰 **Amount Paid**: {payment.total_amount/100:.2f} {payment.currency}\n"
            f"🔋 **Total Credits**: {updated_user['credits_remaining']}\n\n"
            f"You can now continue analyzing your food photos!",
            parse_mode="Markdown",
            reply_markup=create_payment_success_keyboard()
        )
        
        logger.info(f"Payment processed successfully for user {user_id}: {plan['credits']} credits added, total credits: {updated_user['credits_remaining']}")
        
    except Exception as e:
        logger.error(f"Failed to process successful payment for user {telegram_user_id}: {e}")
        import traceback
        logger.error(f"Payment error traceback: {traceback.format_exc()}")
        await message.answer("❌ Payment was successful but there was an error adding credits. Please contact support.") 