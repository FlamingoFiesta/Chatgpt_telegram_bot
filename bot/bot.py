import io
import logging
import asyncio
import traceback
import html
import json
from datetime import datetime, timedelta
import openai

import stripe
import telegram
from telegram import (
    Update,
    User,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    AIORateLimiter,
    filters
)
from telegram.constants import ParseMode, ChatAction

import config
import database
import openai_utils

import base64
import aioredis
import threading
import json
from json import JSONEncoder
import io
import requests
from telegram import InputFile
import pytz

# setup
db = database.Database()

logger = logging.getLogger(__name__)

user_semaphores = {}
user_tasks = {}


HELP_MESSAGE = """Commands:

⚪ /new – Start new dialog 
⚪ /retry – Regenerate last bot answer 
⚪ /mode – Select chat mode 
⚪ /balance – Show balance 
⚪ /topup – Add credits to your account 
⚪ /settings – Show settings 
⚪ /help – Show the commands
⚪ /role – Show your role 

🎨 Generate images from text prompts in <b>👩‍🎨 Artist</b> /mode
👥 Add bot to <b>group chat</b>: /help_group_chat
🎤 You can send <b>Voice Messages</b> instead of text
⌨️ Generate transcripts from voice messages in <b> ⌨️ Stenographer</b> /mode

Important notes:\n
1. The <b>longer</b> your dialog, the <b>more tokens</b> are spent with each new message, <i><b>I remember our conversation!</b></i> \nTo start a <b>new dialog</b>, send the /new command\n
2. <b>Cyber Dud</b> is the default <b>blank mode</b>, it has no special instructions as to how to act. Experiment with the other <b>modes</b> and see which one suits you best!

"""
#add "(see <b>video</b> below)" after instructions if you have the video set up
HELP_GROUP_CHAT_MESSAGE = """You can add bot to any <b>group chat</b> to help and entertain its participants!

Instructions:
1. Add the bot to the group chat
2. Make it an <b>admin</b>, so that it can see messages (all other rights can be restricted)
3. You're awesome!

To get a reply from the bot in the chat – @ <b>tag</b> it or <b>reply</b> to its message.
For example: "{bot_username} write a poem about Telegram"
"""

def update_user_roles_from_config(db, roles):
    for role, user_ids in roles.items():
        for user_id in user_ids:
            db.user_collection.update_one(
                {"_id": user_id},
                {"$set": {"role": role}}
            )
    print("User roles updated from config.")

def split_text_into_chunks(text, chunk_size):
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]

def configure_logging():
    # Configure logging based on the enable_detailed_logging value
    if config.enable_detailed_logging:
        logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
    else:
        logging.basicConfig(level=logging.CRITICAL, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')

    # Set the logger level based on configuration
    logger.setLevel(logging.getLogger().level)

async def register_user_if_not_exists(update: Update, context: CallbackContext, user: User):
    user_registered_now = False
    if not db.check_if_user_exists(user.id):
        db.add_new_user(
            user.id,
            update.message.chat_id,
            username=user.username,
            first_name=user.first_name,
            last_name= user.last_name
        )
        user_registered_now = True
        db.start_new_dialog(user.id)

    if db.get_user_attribute(user.id, "current_dialog_id") is None:
        db.start_new_dialog(user.id)

    if user.id not in user_semaphores:
        user_semaphores[user.id] = asyncio.Semaphore(1)

    if db.get_user_attribute(user.id, "current_model") is None:
        db.set_user_attribute(user.id, "current_model", config.models["available_text_models"][0])

    # back compatibility for n_used_tokens field
    n_used_tokens = db.get_user_attribute(user.id, "n_used_tokens")
    if isinstance(n_used_tokens, int) or isinstance(n_used_tokens, float):  # old format
        new_n_used_tokens = {
            "gpt-4-1106-preview": {
                "n_input_tokens": 0,
                "n_output_tokens": n_used_tokens
            }
        }
        db.set_user_attribute(user.id, "n_used_tokens", new_n_used_tokens)

    # voice message transcription
    if db.get_user_attribute(user.id, "n_transcribed_seconds") is None:
        db.set_user_attribute(user.id, "n_transcribed_seconds", 0.0)

    # image generation
    if db.get_user_attribute(user.id, "n_generated_images") is None:
        db.set_user_attribute(user.id, "n_generated_images", 0)

    if user_registered_now:
        # Notify admins that a new user has just registered
        username = user.username or "No username"
        first_name = user.first_name or "No first name"
        last_name = user.last_name or "No last name"
        notification_text = f"A new user has just registered!\n\nUsername: {username}\nFirst Name: {first_name}\nLast Name: {last_name}"
        for admin_id in config.roles['admin']:
            try:
                await context.bot.send_message(chat_id=admin_id, text=notification_text)
            except Exception as e:
            # Log the error or handle it appropriately
                print(f"Failed to send registration to admin: {str(e)}\n\n Don't worry, this doesn't affect you in anyway!")


async def is_bot_mentioned(update: Update, context: CallbackContext):
     try:
         message = update.message

         if message.chat.type == "private":
             return True

         if message.text is not None and ("@" + context.bot.username) in message.text:
             return True

         if message.reply_to_message is not None:
             if message.reply_to_message.from_user.id == context.bot.id:
                 return True
     except:
         return True
     else:
         return False


async def start_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id

    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    db.start_new_dialog(user_id)

    developer = config.developer_username
    developer_info = ' '.join(developer) if isinstance(developer, list) else developer

    reply_text = "👋 Heyoo! I'm <b>Chatdud</b>, your friendly neighborhood chatbot. Nice to meet ya! \n\n"
    reply_text += "     I'm a telegram bot 🤖 powered by <b>ChatGPT</b> and maybe <b>Claude</b>, and I'm here to help with any questions you might have. \n\n"
    reply_text += "You might ask yourself:\n  <i><b>Why use this bot when I can just use ChatGPT in my browser?</b></i> 🤔\n\n"
    reply_text += "  Well, I use a <b>top-up</b> balance system, meaning you can pay as you go. Don't worry about no monthly $20 subscription!\n\n"
    reply_text += "Also, there is <b>no message limit</b> per hour. As long as you have at least <b>€1.25</b> to feed me, we can chat <b>as much as you want!</b>\n Ain’t that cool?? 😎\n\n"
    reply_text += " 🤫 Psst!\nDon't tell my creator, buut <b>the first euro is on the house!</b> \n\n You have plenty of time to decide if you want to continue using me and support us both. <b>We really appreciate it!</b> 🥰 \n\n"
    reply_text += f"I'm currently in development, for any <b>issues</b> or <b>feedback</b>, {'don’t hesitate to contact my developer ' + developer_info if developer_info else ' '} \n\n"
    reply_text += HELP_MESSAGE

    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)
    await show_chat_modes_handle(update, context)


async def help_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    await update.message.reply_text(HELP_MESSAGE, parse_mode=ParseMode.HTML)


async def help_group_chat_handle(update: Update, context: CallbackContext):
     await register_user_if_not_exists(update, context, update.message.from_user)
     user_id = update.message.from_user.id
     db.set_user_attribute(user_id, "last_interaction", datetime.now())

     text = HELP_GROUP_CHAT_MESSAGE.format(bot_username="@" + context.bot.username)

     await update.message.reply_text(text, parse_mode=ParseMode.HTML)
     #await update.message.reply_video(config.help_group_chat_video_path) remove the comment if you want the video to be sent


#use if you want to check for tokens
async def token_balance_preprocessor(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    current_balance = db.check_token_balance(user_id)
    user_role = db.get_user_role(user_id)

    if user_role == "admin":
        return True

    if db.check_token_balance(user_id) < 100:  # Number of minimum tokens needed
        context.user_data['process_allowed'] = False
        await update.message.reply_text(
            f"_Oops, your balance is too low :( Please top up to continue._ \n\n Your current balance is {current_balance}",
            parse_mode='Markdown'
        )
        return False
    else:
        context.user_data['process_allowed'] = True
        return True

async def euro_balance_preprocessor(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    current_euro_balance = db.get_user_euro_balance(user_id)  
    minimum_euro_required = 0.01  # Set the minimum required balance in euros. This value should be dynamic based on the operation.

    if current_euro_balance < minimum_euro_required:  
        context.user_data['process_allowed'] = False
        await update.message.reply_text(
            f"Oops, your balance is too low :( Please top up to continue. Your current euro balance is €{current_euro_balance:.2f}",
            parse_mode='Markdown'
        )
        return False
    else:
        context.user_data['process_allowed'] = True
        return True


async def retry_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return
    
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    #for tokens
    #if not await token_balance_preprocessor(update, context):
        #return
    if not await euro_balance_preprocessor(update, context):
        return

    dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
    if len(dialog_messages) == 0:
        await update.message.reply_text("No message to retry 🤷‍♂️")
        return


    last_dialog_message = dialog_messages.pop()
    db.set_dialog_messages(user_id, dialog_messages, dialog_id=None)  # last message was removed from the context
    """ #APPARENTLY THIS BREAKS THE FUNCTION, keeping it in case I decide to fix it
    try:
        chatgpt_instance = openai_utils.ChatGPT(model=db.get_user_attribute(user_id, "current_model"))
        answer, (n_input_tokens, n_output_tokens), _ = await chatgpt_instance.send_message(
            message=last_dialog_message["user"],
            dialog_messages=dialog_messages[:-1],  # Exclude the last message for retry
            chat_mode=db.get_user_attribute(user_id, "current_chat_mode")
        )
        # Deduct tokens based on the tokens used for the query and response
        #db.deduct_tokens_based_on_role(user_id, n_input_tokens, n_output_tokens)

        action_type = db.get_user_attribute(user_id, "current_model")  # This assumes the action type can be determined by the model
        db.deduct_cost_for_action(user_id=user_id, action_type=action_type, action_params={'n_input_tokens': n_input_tokens, 'n_output_tokens': n_output_tokens})  
       
        # Now handle the response as needed, e.g., sending it back to the user
        #await update.message.reply_text(answer)
        except Exception as e:
            await update.message.reply_text(f"Error retrying message: {str(e)}")

    action_type = db.get_user_attribute(user_id, "current_model")  # This assumes the action type can be determined by the model
    db.deduct_cost_for_action(user_id=user_id, action_type=action_type, action_params={'n_input_tokens': n_input_tokens, 'n_output_tokens': n_output_tokens})
# APPARENTLY THIS BREAKS THE FUNCTION
    """
    await message_handle(update, context, message=last_dialog_message["user"], use_new_dialog_timeout=False)

#for errors
class CustomEncoder(JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            # Format date in ISO 8601 format, or any format you prefer
            return obj.isoformat()
        # Let the base class default method raise the TypeError
        return JSONEncoder.default(self, obj)

async def _vision_message_handle_fn(
    update: Update, context: CallbackContext, use_new_dialog_timeout: bool = True
):
    logger.info('_vision_message_handle_fn')
    user_id = update.message.from_user.id
    current_model = db.get_user_attribute(user_id, "current_model")

    if current_model != "gpt-4-vision-preview":
        await update.message.reply_text(
            "🥲 Images processing is only available for the <b>GPT-4 Vision</b> model. Please change your settings in /settings",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")

    # new dialog timeout
    if use_new_dialog_timeout:
        if (datetime.now() - db.get_user_attribute(user_id, "last_interaction")).seconds > config.new_dialog_timeout and len(db.get_dialog_messages(user_id)) > 0:
            db.start_new_dialog(user_id)
            await update.message.reply_text(f"Starting new dialog due to timeout (<b>{config.chat_modes[chat_mode]['name']}</b> mode) ✅", parse_mode=ParseMode.HTML)
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    transcribed_text = ''

    # Check for a voice message and transcribe if present
    if update.message.voice:
        voice = update.message.voice
        voice_file = await context.bot.get_file(voice.file_id)

        # Store the file in memory, not on disk
        buf = io.BytesIO()
        await voice_file.download_to_memory(buf)
        buf.name = "voice.oga"
        buf.seek(0)

        # Transcribe the audio
        transcribed_text = await openai_utils.transcribe_audio(buf)
        transcribed_text = transcribed_text.strip()

    buf = None
    
    if update.message.photo:
        photo = update.message.photo[-1] 
        photo_file = await context.bot.get_file(photo.file_id)

        # store file in memory, not on disk
        buf = io.BytesIO()
        await photo_file.download_to_memory(buf)
        buf.name = "image.jpg"  # file extension is required
        buf.seek(0)  # move cursor to the beginning of the buffer

    # in case of CancelledError
    n_input_tokens, n_output_tokens = 0, 0

    try:
        # send placeholder message to user
        placeholder_message = await update.message.reply_text("<i>Making shit up...</i>", parse_mode=ParseMode.HTML)
        message = update.message.caption or update.message.text or transcribed_text or ''

        # send typing action
        await update.message.chat.send_action(action="typing")

        dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
        parse_mode = {"html": ParseMode.HTML, "markdown": ParseMode.MARKDOWN}[
            config.chat_modes[chat_mode]["parse_mode"]
        ]

        chatgpt_instance = openai_utils.ChatGPT(model=current_model)
        if config.enable_message_streaming:
            gen = chatgpt_instance.send_vision_message_stream(
                message,
                dialog_messages=dialog_messages,
                image_buffer=buf,
                chat_mode=chat_mode,
            )
        else:
            (
                answer,
                (n_input_tokens, n_output_tokens),
                n_first_dialog_messages_removed,
            ) = await chatgpt_instance.send_vision_message(
                message,
                dialog_messages=dialog_messages,
                image_buffer=buf,
                chat_mode=chat_mode,
            )

            async def fake_gen():
                yield "finished", answer, (
                    n_input_tokens,
                    n_output_tokens,
                ), n_first_dialog_messages_removed

            gen = fake_gen()

        prev_answer = ""
        async for gen_item in gen:
            (
                status,
                answer,
                (n_input_tokens, n_output_tokens),
                n_first_dialog_messages_removed,
            ) = gen_item

            answer = answer[:4096]  # telegram message limit

            # update only when 100 new symbols are ready
            if abs(len(answer) - len(prev_answer)) < 100 and status != "finished":
                continue

            try:
                await context.bot.edit_message_text(
                    answer,
                    chat_id=placeholder_message.chat_id,
                    message_id=placeholder_message.message_id,
                    parse_mode=parse_mode,
                )
            except telegram.error.BadRequest as e:
                if str(e).startswith("Message is not modified"):
                    continue
                else:
                    await context.bot.edit_message_text(
                        answer,
                        chat_id=placeholder_message.chat_id,
                        message_id=placeholder_message.message_id,
                    )

            await asyncio.sleep(0.01)  # wait a bit to avoid flooding

            prev_answer = answer

        # update user data
        if buf is not None:
            base_image = base64.b64encode(buf.getvalue()).decode("utf-8")
            new_dialog_message = {"user": [
                        {
                            "type": "text",
                            "text": message,
                        },
                        {
                            "type": "image",
                            "image": base_image,
                        }
                    ]
                , "bot": answer, "date": datetime.now()}


        else:
            #new_dialog_message = {"user": [{"type": "text", "text": message}], "bot": answer, "date": datetime.now()} #repo
            new_dialog_message = {"user": message, "bot": answer, "date": datetime.now()}#the test this works
            #HERE IS THE VISION ISSUE
        
        db.set_dialog_messages(
            user_id,
            db.get_dialog_messages(user_id, dialog_id=None) + [new_dialog_message],
            dialog_id=None
        )

        db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)

        action_type = db.get_user_attribute(user_id, "current_model") 
        db.deduct_cost_for_action(user_id=user_id, action_type=action_type, action_params={'n_input_tokens': n_input_tokens, 'n_output_tokens': n_output_tokens}) 

    except asyncio.CancelledError:
        # note: intermediate token updates only work when enable_message_streaming=True (config.yml)
        db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)
        raise

    except Exception as e:
        error_text = f"Something went wrong during completion_1. Reason: {e}" #edit, FIRST_ISSUE
        logger.error(error_text)
        await update.message.reply_text(error_text)
        return

async def unsupport_message_handle(update: Update, context: CallbackContext, message=None):

    if not await is_bot_mentioned(update, context):
        return

    error_text = f"I don't know how to read files or videos. Send the picture in normal mode (Quick Mode)."
    logger.error(error_text)
    await update.message.reply_text(error_text)
    return

#custom commands
async def show_user_role(update: Update, context: CallbackContext):
    user_id = update.effective_user.id

    # Fetch the user's role from the database
    user_role = db.get_user_role(user_id)

    # Send a message to the user with their role
    await update.message.reply_text(f"Your current role is ~ `{user_role}` ~  \n\n Pretty neat huh?", parse_mode='Markdown')

async def show_user_model(update: Update, context: CallbackContext):
    user_id = update.effective_user.id

    # Fetch the user's role from the database
    user_model = db.get_user_model(user_id)

    # Send a message to the user with their role
    await update.message.reply_text(f"Your current model is ~ `{user_model}` ~", parse_mode='Markdown')

async def token_balance_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    token_balance = db.check_token_balance(user_id)
    await update.message.reply_text(f"Your current token balance is: `{token_balance}`", parse_mode='Markdown')

async def topup_handle(update: Update, context: CallbackContext, chat_id=None):

    user_id = chat_id if chat_id else update.effective_user.id
    
    if config.stripe_secret_key is None or config.stripe_secret_key == "":

        await context.bot.send_message(
            chat_id=user_id,
            text="This bot does not have the payment system turned on :(", 
            parse_mode='Markdown'
        )
        return

    # Define euro amount options for balance top-up
    euro_amount_options = {
        "€1.25": 125,  # Pay €1.25 and add €1 to balance
        "€3": 300,  # Add €3 to balance
        "€5": 500,  # Add €5 to balance
        "€10": 1000,  # Add €10 to balance
        "€20": 2000,  # Add €20 to balance
        "Other amount...": "custom",  # Custom amount option
        "Donation ❤️": "donation"
    }
    
    # Generate inline keyboard buttons for each euro amount option
    keyboard = [
        [InlineKeyboardButton(text, callback_data=f"topup|topup_{amount}")]
        for text, amount in euro_amount_options.items()
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await context.bot.send_photo(chat_id=user_id, photo=open(config.payment_banner_photo_path, 'rb')) #Send the banner

    # Send message with euro amount options
    await context.bot.send_message(
        chat_id=user_id,
        text="Currently supported payment methods: *Card*, *GooglePay*, *PayPal*, *iDeal*.\n\n For *GPT-4*, *€1* gives you *75,000* words, or *200 A4 pages*!\n\n For *GPT-3.5*, its almost *20 times cheaper*. \n\nPlease select the *amount* you wish to add to your *balance*:\n\n", #topup 1.25 message
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def topup_callback_handle(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    
    data = query.data

    #context.user_data['is_donation'] = False

    if data == "topup|topup_custom" or data == "topup|topup_donation":
        #custom_type = "donation" if "donation" in data else "custom"
        is_donation = "donation" in data
        prompt_text = "Thank you for considering *donating*! \n\nPlease enter the *donation* amount in euros(e.g., *5* for *€5*):" if is_donation == "donation" else "Please enter the *custom amount* in euros (e.g., *5* for *€5*):"
        # Prompt the user to enter a custom amount
        keyboard = [[InlineKeyboardButton("⬅️", callback_data="topup|back_to_topup_options")]]
        await query.edit_message_text(
            text=prompt_text,
            reply_markup=InlineKeyboardMarkup([]), #write keyboard instead of the brackets "[]" if you want the button
            parse_mode='Markdown'
        )
        
        context.user_data['awaiting_custom_topup'] = "donation" if is_donation else "custom" # Store a flag in the user's context to indicate awaiting a custom top-up amount
        context.user_data['is_donation'] = is_donation # store a flag in the user's context to differentiate between donation and others

        return

    elif data == "topup|back_to_topup_options":
        
        context.user_data['awaiting_custom_topup'] = False
        context.user_data.pop('is_donation', None)
            # Define euro amount options for balance top-up
        euro_amount_options = {
            "€1.25": 125,  # Example: Add €10 to balance
            "€3": 300,  # Example: Add €10 to balance
            "€5": 500,  # Example: Add €10 to balance
            "€10": 1000,  # Example: Add €20 to balance
            "€20": 2000,  # Example: Add €50 to balance
            "Other amount...": "custom",  # Custom amount option
            "Donation ❤️": "donation"
        }

    # Generate inline keyboard buttons for each euro amount option
        keyboard = [
            [InlineKeyboardButton(text, callback_data=f"topup|topup_{amount if amount != 'custom' else 'custom'}")]
            for text, amount in euro_amount_options.items()
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

    # Replace the existing message with the top-up options message
        await query.edit_message_text(
            text="Currently supported payment methods: *Card*, *GooglePay*, *PayPal*, *iDeal*.\n\n For *GPT-4*, *€1* gives you *75,000* words, or *200 A4 pages*!\n\n For *GPT-3.5*, its almost *20 times cheaper*. \n\nPlease select the *amount* you wish to add to your *balance*:\n\n",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        ) 

    else:
        
        await query.edit_message_text("⏳ Generating payment link...")
        context.user_data.pop('is_donation', None)
        user_id = update.effective_user.id
        _, amount_str = query.data.split("_")
        amount_cents = int(amount_str)  # Amount in cents for Stripe

        session_url = await create_stripe_session(user_id, amount_cents, context)

    # Conditional warning for the €1.25 top-up
        if amount_cents == 125:  # Check if the amount is 125 cents (€1.25)                                                    
            warning_message = "\n\n*Note:* Stripe charges a *€0.25 fee* per transaction. Therefore, you'll receive *€1.00* in credit so that I don't end up loosing money. \nFor all other payment options, I'll take care of the tax for you. \n*Thank you* for understanding! ❤️"
        else:
            warning_message = ""

        payment_text = (
        f"Tap the button below to complete your *€{amount_cents / 100:.2f}* payment! {warning_message}\n\n"
        "🔐 The bot uses a *trusted* payment service [Stripe](https://stripe.com/legal/ssa). "
        "*It does not store your payment data.* \n\nOnce you make a payment, you will receive a *confirmation message*!"
        )
        keyboard = [
        [InlineKeyboardButton("💳Pay", url=session_url)],
        [InlineKeyboardButton("⬅️", callback_data="topup|back_to_topup_options")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(text=payment_text, parse_mode='Markdown', reply_markup=reply_markup, disable_web_page_preview=True)

async def create_stripe_session(user_id: int, amount_cents: int, context: CallbackContext):
    stripe.api_key = config.stripe_secret_key
    is_donation = context.user_data.get('is_donation', False)
    product_name = "Donation❤️" if is_donation else "Balance Top-up"
    session = stripe.checkout.Session.create(
        payment_method_types=['card', 'paypal', 'ideal'],
        line_items=[{
            'price_data': {
                'currency': 'eur',
                'product_data': {'name': product_name},
                'unit_amount': amount_cents,
            },
            'quantity': 1,
        }],
        mode='payment',
        success_url='https://t.me/ChatdudBot',  # Adjust with your success URL
        cancel_url='https://t.me/ChatdudBot',  # Adjust with your cancel URL
        metadata={'user_id': user_id, 'is_donation': str(is_donation).lower()}, # Metadata to track which user is making the payment
    )
    return session.url

async def send_confirmation_message_async(user_id, euro_amount, is_donation):
    user = db.user_collection.find_one({"_id": user_id})
    if user:
        chat_id = user["chat_id"]

        if is_donation:
            message = f"Thank you *so much* for your generous donation of *€{euro_amount:.2f}*! Your support is *greatly appreciated*!! ❤️❤️"
            
        else:
            message = f"Your top-up of *€{euro_amount:.2f}* was *successful!*🎉 \n\nYour new balance will be updated shortly."
            if user.get("role") == "trial_user":
                db.user_collection.update_one(
                    {"_id": user_id},
                    {"$set": {"role": "regular_user"}}
                )
                message += "\n\nYou have been upgraded to the role of *regular user*! Thank you *so much* for supporting this project, you're *amazing*! ❤️"

        await bot_instance.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')


def start_asyncio_loop():

    if config.stripe_webhook_secret is None or config.stripe_webhook_secret == "":
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_redis_listener())
    loop.run_forever()

async def start_redis_listener():

    if config.stripe_webhook_secret is None or config.stripe_webhook_secret == "":
        return

    # For aioredis version 2.x, connect to Redis using the new method
    redis = aioredis.from_url("redis://redis:6379", encoding="utf-8", decode_responses=True)
    
    async with redis.client() as client:
        sub = client.pubsub()
        await sub.subscribe('payment_notifications')
        
        async for msg in sub.listen():
            # Process messages
            if msg['type'] == 'message':
                data = json.loads(msg['data'])
                user_id = data['user_id']
                euro_amount = data['euro_amount']
                is_donation = data.get('is_donation', False)
                await send_confirmation_message_async(user_id, euro_amount, is_donation)

#admin commands
async def admin_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id

    # Check if the user has an admin role
    user_role = db.get_user_role(user_id)

    if user_id not in config.roles['admin']:
        await update.message.reply_text("You're not allowed to use this command.")
        return

    # List of admin commands
    admin_commands = [
        "",
        "/admin - List available admin commands",
        "/get_user_count - Get the number of users",
        "/list_user_roles - List users and their role",
        "/change_role - Works even if youre not currently admin role",
        "",
        "Messaging commands:",
        "",
        "/send_message_to_id <user_id> <message>",
        "/message_username <user_username> <message>",
        "/message_name <user_first_name> <message>",
        "/message_role <user_role> <message>",
        "/message_all <message> - Message the entire database",
        # Add more admin commands here
    ]
    commands_text = "\n".join(admin_commands)
    await update.message.reply_text(f"Available admin commands:\n{commands_text}") #, parse_mode='Markdown'

async def get_user_count(update, context):
    user_id = update.effective_user.id

    if user_id not in config.roles['admin']:
        await update.message.reply_text("You're not allowed to use this command.")
        return

    user_count = db.get_user_count()  
    await update.message.reply_text(f"Total number of users: {user_count}")

async def list_user_roles(update, context):
    user_id = update.effective_user.id
    local_timezone= config.timezone
    LOCAL_TIMEZONE = pytz.timezone(local_timezone)

    # Check if the user has the admin role
    if user_id not in config.roles['admin']:
        await update.message.reply_text("You're not allowed to use this command.")
        return

    users_and_roles = db.get_users_and_roles()
    message_lines = []

    for user in users_and_roles:
        username = user.get('username', 'No Username')
        first_name = user.get('first_name', 'No First Name')
        role = user.get('role', 'No Role')
        last_interaction = user.get('last_interaction')

        # Adjusting the datetime
        if last_interaction:
            last_interaction = last_interaction.replace(tzinfo=pytz.UTC)  # Ensure it has UTC timezone
            local_last_interaction = last_interaction.astimezone(LOCAL_TIMEZONE)
            now_local = datetime.now(LOCAL_TIMEZONE)

            if local_last_interaction.date() == now_local.date():
                last_interaction_str = local_last_interaction.strftime('%H:%M')
            else:
                days_ago = (now_local.date() - local_last_interaction.date()).days
                last_interaction_str = f"{days_ago} days ago"
        else:
            last_interaction_str = 'No Time'

        message_lines.append(
            f"`{username}` | `{first_name}` | `{role}` | `{last_interaction_str}`"
        )

    message_text = "\n\n".join(message_lines)

    await update.message.reply_text(message_text, parse_mode='Markdown')

async def send_message_to_id(update: Update, context: CallbackContext):
    user_id = update.effective_user.id

    # Check if the user has the admin role
    if user_id not in config.roles['admin']:
        await update.message.reply_text("You're not allowed to use this command.")
        return

    # Extract user_id and message from the command
    try:
        _, target_user_id, *message_parts = update.message.text.split()
        message_text = " ".join(message_parts)
        target_user_id = int(target_user_id)  # Ensure it's an integer
    except (ValueError, IndexError):
        await update.message.reply_text("Usage: /send_message_to_user <user_id> <message>")
        return

    # Use the bot object to send a message to the target user
    try:
        await context.bot.send_message(chat_id=target_user_id, text=message_text)
        await update.message.reply_text(f"Message sent to user {target_user_id}.")
    except Exception as e:
        await update.message.reply_text(f"Failed to send message: {str(e)}")

async def send_message_to_username(update: Update, context: CallbackContext):
    user_id = update.effective_user.id

    # Check if the user has the admin role
    if user_id not in config.roles['admin']:
        await update.message.reply_text("You're not allowed to use this command.")
        return

    try:
        _, username, *message_parts = update.message.text.split()
        message_text = " ".join(message_parts)
    except ValueError:
        await update.message.reply_text("Usage: /send_message_to_username <username> <message>")
        return

    # Find the user in the database by username
    target_user = db.find_user_by_username(username.replace("@", ""))
    if not target_user:
        await update.message.reply_text(f"User {username} not found.")
        return

    # Send message
    try:
        await context.bot.send_message(chat_id=target_user["_id"], text=message_text)
        await update.message.reply_text(f"Message sent to {username}.")
    except Exception as e:
        await update.message.reply_text(f"Failed to send message: {str(e)}")

async def send_message_to_name(update: Update, context: CallbackContext):
    user_id = update.effective_user.id

    if user_id not in config.roles['admin']:
        await update.message.reply_text("You're not allowed to use this command.")
        return

    try:
        _, first_name, *message_parts = update.message.text.split()
        message_text = " ".join(message_parts)
    except ValueError:
        await update.message.reply_text("Usage: /send_message_to_name <first_name> <message>")
        return

    # Find users by first name
    users = db.find_users_by_first_name(first_name)
    if not users:
        await update.message.reply_text(f"No users found with the first name {first_name}.")
        return

    # Send message to each user
    for user in users:
        try:
            await context.bot.send_message(chat_id=user["_id"], text=message_text)
        except Exception as e:
            # Log or handle individual send errors
            continue
    await update.message.reply_text(f"Message sent to users with the first name {first_name}.")

async def send_message_to_role(update: Update, context: CallbackContext):
    user_id = update.effective_user.id

    if user_id not in config.roles['admin']:
        await update.message.reply_text("You're not allowed to use this command.")
        return

    try:
        _, role, *message_parts = update.message.text.split()
        message_text = " ".join(message_parts)
    except ValueError:
        await update.message.reply_text("Usage: /send_message_to_role <role> <message>")
        return

    # Find users by role
    users = db.find_users_by_role(role)
    if not users:
        await update.message.reply_text(f"No users found with the role {role}.")
        return

    formatted_message_text = f"_{message_text}_"
    #formatted_message_text = f"<b><i>{message_text}</i></b>" #html
    # Send message to each user
    for user in users:
        try:
            await context.bot.send_message(chat_id=user["_id"], text=formatted_message_text, parse_mode='Markdown')
        except Exception as e:
            # Log or handle individual send errors
            continue
    await update.message.reply_text(f"Message sent to users with the role {role}.")

async def send_message_to_all(update: Update, context: CallbackContext):
    user_id = update.effective_user.id

    # Check if the user has the permission to send a broadcast
    if user_id not in config.roles['admin']:
        await update.message.reply_text("You're not allowed to use this command.", parse_mode='Markdown')
        return

    try:
        _, *message_parts = update.message.text.split(maxsplit=1)  # Split only once to get the whole message after the command
        message_text = message_parts[0] if message_parts else "No message provided."
    except ValueError:
        await update.message.reply_text("Usage: /message_all <message>", parse_mode='Markdown')
        return

    # Retrieve all users' IDs from the database
    users_ids = db.get_all_user_ids()  # This function now correctly returns just the user IDs
    if not users_ids:
        await update.message.reply_text("No users found in the database.", parse_mode='Markdown')
        return

    # Send message to each user ID
    failed_count = 0
    failed_users = []

    for user_id in users_ids:
        try:
            await context.bot.send_message(chat_id=user_id, text=message_text, parse_mode='Markdown')
        except Exception as e:
            user_details = db.get_user_by_id(user_id)
            if user_details:
                failed_users.append(user_details.get('first_name', 'Unknown User'))

            failed_count += 1
            failed_users.append(str(user_id))
            # Log or handle individual send errors

    success_message = f"Message sent to all users. Failures: {failed_count}" if failed_count else "Message successfully sent to all users."
    failed_users_message = f"\n\nFailed to send message to {failed_count} users: {', '.join(failed_users)}" if failed_users else ""
    final_message = success_message + failed_users_message

    await update.message.reply_text(final_message, parse_mode='Markdown')

async def change_role(update: Update, context: CallbackContext):
    user_id = update.effective_user.id

    # Assuming 'roles' is a dictionary in your config with user roles and their corresponding user IDs
    if user_id not in config.roles['admin']:
        await update.message.reply_text("You're not allowed to use this command.")
        return

    # Fetch the current user's role
    user_data = db.user_collection.find_one({"_id": user_id})
    current_role = user_data.get("role", "No role set") if user_data else "No user data found"

    # Define available roles
    roles = ["admin", "beta_tester", "friend",  "regular_user", "trial_user"]

    # Generate buttons for each role, marking the current role with a checkmark
    keyboard = [
        [InlineKeyboardButton(f"{role} {'✅' if role == current_role else ''}", callback_data=f"set_role|{role}")]
        for role in roles
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Send message with role options
    await update.message.reply_text(
        "Please choose a role to switch to:",
        reply_markup=reply_markup
    )

async def handle_role_change(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    if data.startswith('set_role|'):
        new_role = data.split('|')[1]
        
        # Update the user's role in the database
        db.user_collection.update_one(
            {"_id": user_id},
            {"$set": {"role": new_role}}
        )
        
        # Fetch the updated role list with the current role now being the new_role
        roles = ["admin", "beta_tester", "friend",  "regular_user", "trial_user"]
        
        # Regenerate keyboard with updated checkmark
        keyboard = [
            [InlineKeyboardButton(f"{role} {'✅' if role == new_role else ''}", callback_data=f"set_role|{role}")]
            for role in roles
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Update the message with the new keyboard
        await query.edit_message_text(
            text="Please choose a role to switch to:",
            reply_markup=reply_markup
        )

# end of admin commands

async def message_handle(update: Update, context: CallbackContext, message=None, use_new_dialog_timeout=True):
    # check if bot was mentioned (for group chats)
    if not await is_bot_mentioned(update, context):
        return

    # check if message is edited
    if update.edited_message is not None:
        await edited_message_handle(update, context)
        return  

    _message = message or update.message.text

    # remove bot mention (in group chats)
    if update.message.chat.type != "private":
        _message = _message.replace("@" + context.bot.username, "").strip()

    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")

    if not await euro_balance_preprocessor(update, context):
        return

    if chat_mode == "artist":
        await generate_image_handle(update, context, message=message)
        return
    
    if chat_mode == "stenographer":
        await voice_message_handle(update, context, message=message)
        return

    current_model = db.get_user_attribute(user_id, "current_model")


    #custom top up
    if 'awaiting_custom_topup' in context.user_data and context.user_data['awaiting_custom_topup']:
        user_input = update.message.text.replace(',', '.')
        try:
            custom_amount_euros = float(user_input)

            min_amount = 3
            error_message = "The *minimum* amount for a *custom top-up* is *€3*. Please enter a *valid* amount."

            # Adjust minimum amount and error message for donations
            if context.user_data['awaiting_custom_topup'] == "donation":
                min_amount = 1
                error_message = "The *minimum* amount for a *donation* is *€1*. Please enter a *valid* amount."

            if custom_amount_euros < min_amount: #mininum ammount custom
                keyboard = [[InlineKeyboardButton("⬅️", callback_data="topup|back_to_topup_options")]]
                await context.bot.send_message(
                    chat_id=update.effective_user.id,
                    text=f"{error_message}\n\n Press the *back button* to return to *top-up options*",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='Markdown'
                )
                return  # Stop further processing to prevent sending a payment link

            placeholder_message = await update.message.reply_text("⏳ Generating payment *link*...", parse_mode='Markdown')
            placeholder_message_id = placeholder_message.message_id

            custom_amount_cents = int(custom_amount_euros * 100)
        
        # Now create a Stripe session for this custom amount
            payment_url = await create_stripe_session(update.effective_user.id, custom_amount_cents, context)
        
            thank_you_message = "\n\nThank you so much for your *donation*! ❤️" if context.user_data['awaiting_custom_topup'] == "donation" else ""

        # Send the Stripe payment link to the user
            payment_text = (
                f"Tap the button below to complete your *€{custom_amount_euros:.2f}* payment!{thank_you_message}\n\n"
                "🔐The bot uses a *trusted* payment service [Stripe](https://stripe.com/legal/ssa). "
                "*It does not store your payment data.* \n\nOnce you make a payment, you will receive a confirmation message!"
            )
            keyboard = [
                [InlineKeyboardButton("💳Pay", url=payment_url)],
                [InlineKeyboardButton("⬅️", callback_data="topup|back_to_topup_options")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            

            # Update the message with payment information
            await context.bot.edit_message_text(
                chat_id=update.effective_user.id,
                message_id=placeholder_message_id,
                text=payment_text,
                parse_mode='Markdown',
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )

        # Reset the flag
            context.user_data['awaiting_custom_topup'] = False

            return
        
        except ValueError:
        # In case of invalid input, prompt again or handle as needed
            keyboard = [[InlineKeyboardButton("⬅️", callback_data="topup|back_to_topup_options")]]
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text="*Invalid amount* entered. Please enter a *numeric* value in *euros* (e.g., 5 for €5). \n\n Press the *back button* to return to *top-up options*",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
            return

    #chatgpt_instance = openai_utils.ChatGPT(model=current_model)
    async def message_handle_fn():
        
        # new dialog timeout
        if use_new_dialog_timeout:
            if (datetime.now() - db.get_user_attribute(user_id, "last_interaction")).seconds > config.new_dialog_timeout and len(db.get_dialog_messages(user_id)) > 0:
                db.start_new_dialog(user_id)
                await update.message.reply_text(f"Starting new dialog due to timeout (<b>{config.chat_modes[chat_mode]['name']}</b> mode) ✅", parse_mode=ParseMode.HTML)
        db.set_user_attribute(user_id, "last_interaction", datetime.now())

        # in case of CancelledError
        n_input_tokens, n_output_tokens = 0, 0
        

        try:
    
            # send placeholder message to user
            placeholder_message = await update.message.reply_text("<i>Making shit up...</i>", parse_mode=ParseMode.HTML)

            # send typing action
            await update.message.chat.send_action(action="typing")

            if _message is None or len(_message) == 0:
                 await update.message.reply_text("🥲 You sent <b>empty message</b>. Please, try again!", parse_mode=ParseMode.HTML)
                 return

            dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
            parse_mode = {
                "html": ParseMode.HTML,
                "markdown": ParseMode.MARKDOWN
            }[config.chat_modes[chat_mode]["parse_mode"]]

            chatgpt_instance = openai_utils.ChatGPT(model=current_model)

            if config.enable_message_streaming:
                gen = chatgpt_instance.send_message_stream(_message, dialog_messages=dialog_messages, chat_mode=chat_mode)

            else:
                answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed = await chatgpt_instance.send_message(
                    _message,
                    dialog_messages=dialog_messages,
                    chat_mode=chat_mode
                )


                #await context.bot.send_message(chat_id=update.effective_chat.id, text=answer, parse_mode=parse_mode, disable_web_page_preview=True) #repo commit

                async def fake_gen():
                    yield "finished", answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

                gen = fake_gen()

            prev_answer = ""

            async for gen_item in gen:
                status, answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed = gen_item

#                answer = current_model + " " + answer #repo commit
                answer = answer[:4096]  # telegram message limit

                # update only when 100 new symbols are ready
                if abs(len(answer) - len(prev_answer)) < 100 and status != "finished":
                    continue

                try:
                    await context.bot.edit_message_text(answer, chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, parse_mode=parse_mode, disable_web_page_preview=True)
                except telegram.error.BadRequest as e:
                    if str(e).startswith("Message is not modified"):
                        continue

                    else:
                        await context.bot.edit_message_text(answer, chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, disable_web_page_preview=True) #maybe bug

                await asyncio.sleep(0.01)  # wait a bit to avoid flooding

                prev_answer = answer

            # update user data
            #new_dialog_message = {"user": _message, "bot": answer, "date": datetime.now()} #this still works
            new_dialog_message = {"user": [{"type": "text", "text": _message}], "bot": answer, "date": datetime.now()} #repo commit
            #HERE IS THE ISSUE

            db.set_dialog_messages(
                user_id,
                db.get_dialog_messages(user_id, dialog_id=None) + [new_dialog_message],
                dialog_id=None
            )
        
            action_type = db.get_user_attribute(user_id, "current_model") #repo commit #maybe comment this out
            db.deduct_cost_for_action(user_id=user_id, action_type=action_type, action_params={'n_input_tokens': n_input_tokens, 'n_output_tokens': n_output_tokens}) 
        
            db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)

        except asyncio.CancelledError:
            # note: intermediate token updates only work when enable_message_streaming=True (config.yml)
            db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)
            #db.deduct_tokens_based_on_role(user_id, n_input_tokens, n_output_tokens)

            action_type = db.get_user_attribute(user_id, "current_model")  # This assumes the action type can be determined by the model #maybe comment this out
            db.deduct_cost_for_action(user_id=user_id, action_type=action_type, action_params={'n_input_tokens': n_input_tokens, 'n_output_tokens': n_output_tokens}) 

            raise

        except Exception as e:
            error_text = f"Something went wrong during completion 2. Reason: {e}" #edit, SECOND_ISSUE
            logger.error(error_text)
            await update.message.reply_text(error_text)
            return

        # send message if some messages were removed from the context
        if n_first_dialog_messages_removed > 0:
            if n_first_dialog_messages_removed == 1:
                text = "✍️ <i>Note:</i> Your current dialog is too long, so your <b>first message</b> was removed from the context.\n Send /new command to start new dialog"
            else:
                text = f"✍️ <i>Note:</i> Your current dialog is too long, so <b>{n_first_dialog_messages_removed} first messages</b> were removed from the context.\n Send /new command to start new dialog"
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)



    async with user_semaphores[user_id]:
        #task = asyncio.create_task(message_handle_fn())
        #user_tasks[user_id] = task

        if current_model == "gpt-4-vision-preview" or update.message.photo is not None and len(update.message.photo) > 0:
            logger.error('gpt-4-vision-preview')
            if current_model != "gpt-4-vision-preview":
                current_model = "gpt-4-vision-preview"
                db.set_user_attribute(user_id, "current_model", "gpt-4-vision-preview") #this lets you send images to any model and it changes it to vision
            task = asyncio.create_task(
                _vision_message_handle_fn(update, context, use_new_dialog_timeout=use_new_dialog_timeout)
            )
        else:
            task = asyncio.create_task(
                message_handle_fn()
            )            

        user_tasks[user_id] = task


        try:
            await task
        except asyncio.CancelledError:
            await update.message.reply_text("✅ Canceled", parse_mode=ParseMode.HTML)
        else:
            pass
        finally:
            if user_id in user_tasks:
                del user_tasks[user_id]


async def is_previous_message_not_answered_yet(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    if user_semaphores[user_id].locked():
        text = "⏳ Please <b>wait</b> for a reply to the previous message\n"
        text += "Or you can /cancel it"
        await update.message.reply_text(text, reply_to_message_id=update.message.id, parse_mode=ParseMode.HTML)
        return True
    else:
        return False

async def voice_message_handle(update: Update, context: CallbackContext):
    # check if bot was mentioned (for group chats)
    if not await is_bot_mentioned(update, context):
        return

    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    
    #if not await token_balance_preprocessor(update, context):
        #return

    if not await euro_balance_preprocessor(update, context):
        return

    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")

    if chat_mode == "stenographer":
        placeholder_message = await update.message.reply_text("⌨️: <i>Transcribing...</i>", parse_mode=ParseMode.HTML)
    else:
        placeholder_message = await update.message.reply_text("🎤: <i>Transcribing...</i>", parse_mode=ParseMode.HTML)
    

    voice = update.message.voice
    voice_file = await context.bot.get_file(voice.file_id)
    
    # store file in memory, not on disk
    buf = io.BytesIO()
    await voice_file.download_to_memory(buf)
    buf.name = "voice.oga"  # file extension is required
    buf.seek(0)  # move cursor to the beginning of the buffer

    transcribed_text = await openai_utils.transcribe_audio(buf)
    text = f"🎤: <i>{transcribed_text}</i>"

    audio_duration_minutes = voice.duration / 60.0

    # update n_transcribed_seconds
    db.set_user_attribute(user_id, "n_transcribed_seconds", voice.duration + db.get_user_attribute(user_id, "n_transcribed_seconds"))
    #db.deduct_tokens_based_on_role(user_id, n_input_tokens, n_output_tokens)
    db.deduct_cost_for_action(user_id=user_id, action_type='whisper', action_params={'audio_duration_minutes': audio_duration_minutes})

    if chat_mode == "stenographer":
        transcription_message = f"Your transcription is in: \n\n<code>{transcribed_text}</code>"
        await context.bot.edit_message_text(transcription_message, chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, parse_mode=ParseMode.HTML)
        return
    else:
        await context.bot.edit_message_text(text, chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, parse_mode=ParseMode.HTML)

    await message_handle(update, context, message=transcribed_text)

    return transcribed_text

async def generate_image_handle(update: Update, context: CallbackContext, message=None):

    """Generate images based on the user's preferences stored in the database."""
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    # Retrieve user preferences
    user_preferences = db.get_user_attribute(user_id, "image_preferences")

    model = user_preferences.get("model", "dalle-2")
    n_images = user_preferences.get("n_images", 3)
    resolution = user_preferences.get("resolution", "1024x1024")

    # Ensure sufficient balance before proceeding
    if not await euro_balance_preprocessor(update, context):
        return

    # Send typing action
    await update.message.chat.send_action(action="upload_photo")

    message = message or update.message.text

    # Send a placeholder message
    placeholder_message = await update.message.reply_text("<i>Waking up Picasso...</i>", parse_mode=ParseMode.HTML)

    # Generate the images based on user preferences
    try:
        image_urls = await openai_utils.generate_images(prompt=message or update.message.text, model=model, n_images=n_images, size=resolution)
    except openai.error.InvalidRequestError as e:
        if str(e).startswith("Your request was rejected as a result of our safety system"):
            text = "🥲 Your request <b>doesn't comply</b> with OpenAI's usage policies.\nWhat did you write there, huh??"
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
            return
        else:
            logging.error(f"OpenAI Invalid Request Error: {str(e)}")
            text = f"⚠️ There was an issue with your request. Please try again.\n\n<b>Reason</b>: {str(e)}"
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        return
    
    except Exception as e:
        # General error handler for unexpected issues
        logging.error(f"Unexpected Error: {str(e)}")
        text = f"⚠️ An unexpected error occurred. Please try again. \n\n<b>Reason</b>: {str(e)}"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    # Action parameters
    action_params = {
        "model": model,         # DALL-E model 
        "quality": user_preferences.get("quality", "standard"),  # Image quality
        "resolution": resolution,  # Resolution (e.g., 1024x1024)
        "n_images": n_images      # Number of images
    }

    # Token usage and cost deduction
    db.set_user_attribute(user_id, "n_generated_images", n_images + db.get_user_attribute(user_id, "n_generated_images"))
    action_type = user_preferences.get("model", "dalle-2")
    db.deduct_cost_for_action(user_id=user_id, action_type=action_type, action_params=action_params)

    # Update the placeholder message with the final image message
    pre_generation_message = f"Here is my attempt at drawing 🎨:\n\n  <i>{message or ''}</i>  \n\n Hold on, the picture is on its way!"
    await context.bot.edit_message_text(pre_generation_message, chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, parse_mode=ParseMode.HTML)

    # Upload each generated image
    for image_url in image_urls:
        await update.message.chat.send_action(action="upload_photo")
        await upload_image_from_memory(
            bot=context.bot,
            chat_id=update.message.chat_id,
            image_url=image_url
        )
    
    post_generation_message = f"Here is my attempt at drawing 🎨:\n\n  <i>{message or ''}</i>  \n\n Do you like it??"
    await context.bot.edit_message_text(post_generation_message, chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, parse_mode=ParseMode.HTML)

#some resolutions were throwing an error, so I changed to send the image from memory
async def upload_image_from_memory(bot, chat_id, image_url):
    # Download the image to an in-memory buffer
    response = requests.get(image_url, stream=True)
    if response.status_code == 200:
        image_buffer = io.BytesIO(response.content)
        image_buffer.name = "image.jpg"  # Set a name for the file

        # Send the photo using the in-memory buffer
        await bot.send_photo(chat_id=chat_id, photo=InputFile(image_buffer, "image.jpg"))

async def new_dialog_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    current_model = db.get_user_attribute(user_id, "current_model")
    if current_model == "gpt-4-vision-preview":
        db.set_user_attribute(user_id, "current_model", "gpt-4-turbo-2024-04-09")

    #db.set_user_attribute(user_id, "current_model", "gpt-4-turbo-2024-04-09")

    db.start_new_dialog(user_id)
    await update.message.reply_text("Starting new dialog ✅")

    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")
    await update.message.reply_text(f"{config.chat_modes[chat_mode]['welcome_message']}", parse_mode=ParseMode.HTML)

async def cancel_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    if user_id in user_tasks:
        task = user_tasks[user_id]
        task.cancel()
    else:
        await update.message.reply_text("<i>Nothing to cancel...</i>", parse_mode=ParseMode.HTML)

def get_chat_mode_menu(page_index: int):
    n_chat_modes_per_page = config.n_chat_modes_per_page
    text = f"Select <b>chat mode</b> ({len(config.chat_modes)} modes available):"

    # buttons
    chat_mode_keys = list(config.chat_modes.keys())
    page_chat_mode_keys = chat_mode_keys[page_index * n_chat_modes_per_page:(page_index + 1) * n_chat_modes_per_page]

    keyboard = []
    for chat_mode_key in page_chat_mode_keys:
        name = config.chat_modes[chat_mode_key]["name"]
        keyboard.append([InlineKeyboardButton(name, callback_data=f"set_chat_mode|{chat_mode_key}")])

    # pagination
    if len(chat_mode_keys) > n_chat_modes_per_page:
        is_first_page = (page_index == 0)
        is_last_page = ((page_index + 1) * n_chat_modes_per_page >= len(chat_mode_keys))

        if is_first_page:
            keyboard.append([
                InlineKeyboardButton("»", callback_data=f"show_chat_modes|{page_index + 1}")
            ])
        elif is_last_page:
            keyboard.append([
                InlineKeyboardButton("«", callback_data=f"show_chat_modes|{page_index - 1}"),
            ])
        else:
            keyboard.append([
                InlineKeyboardButton("«", callback_data=f"show_chat_modes|{page_index - 1}"),
                InlineKeyboardButton("»", callback_data=f"show_chat_modes|{page_index + 1}")
            ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    return text, reply_markup

async def show_chat_modes_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    text, reply_markup = get_chat_mode_menu(0)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

async def show_chat_modes_callback_handle(update: Update, context: CallbackContext):
     await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
     if await is_previous_message_not_answered_yet(update.callback_query, context): return

     user_id = update.callback_query.from_user.id
     db.set_user_attribute(user_id, "last_interaction", datetime.now())

     query = update.callback_query
     await query.answer()

     page_index = int(query.data.split("|")[1])
     if page_index < 0:
         return

     text, reply_markup = get_chat_mode_menu(page_index)
     try:
         await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
     except telegram.error.BadRequest as e:
         if str(e).startswith("Message is not modified"):
             pass

async def set_chat_mode_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id

    query = update.callback_query
    await query.answer()

    chat_mode = query.data.split("|")[1]

    db.set_user_attribute(user_id, "current_chat_mode", chat_mode)
    db.start_new_dialog(user_id)

    await context.bot.send_message(
        update.callback_query.message.chat.id,
        f"{config.chat_modes[chat_mode]['welcome_message']}",
        parse_mode=ParseMode.HTML
    )

def get_settings_menu(user_id: int):
    text = "⚙️ Settings:"

    # Define the buttons for the settings menu
    keyboard = [
        [InlineKeyboardButton("🧠 AI Model", callback_data='model-ai_model')],
        [InlineKeyboardButton("🎨 Artist Model", callback_data='model-artist_model')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    return text, reply_markup

async def settings_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context):
        return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    text, reply_markup = get_settings_menu(user_id)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

async def set_settings_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id

    query = update.callback_query
    await query.answer()

    _, model_key = query.data.split("|")
    db.set_user_attribute(user_id, "current_model", model_key)

    await display_model_info(query, user_id, context)

async def display_model_info(query, user_id, context):
    current_model = db.get_user_attribute(user_id, "current_model")
    model_info = config.models["info"][current_model]
    description = model_info["description"]
    scores = model_info["scores"]
    
    details_text = f"{description}\n\n"
    for score_key, score_value in scores.items():
        details_text += f"{'🟢' * score_value}{'⚪️' * (5 - score_value)} – {score_key}\n"
    
    details_text += "\nSelect <b>model</b>:"
    
    buttons = []
    claude_buttons = []
    other_buttons = []

    for model_key in config.models["available_text_models"]:
        title = config.models["info"][model_key]["name"]
        if model_key == current_model:
            title = "✅ " + title
        
        # Adjust callback data to include a prefix for Claude models
        if "claude" in model_key.lower():
            callback_data = f"claude-model-set_settings|{model_key}"
            claude_buttons.append(InlineKeyboardButton(title, callback_data=callback_data))
        else:
            callback_data = f"model-set_settings|{model_key}"
            other_buttons.append(InlineKeyboardButton(title, callback_data=callback_data))
    
    half_size = len(other_buttons) // 2
    first_row = other_buttons[:half_size]
    second_row = other_buttons[half_size:]
    back_button = [InlineKeyboardButton("⬅️", callback_data='model-back_to_settings')]
    
    reply_markup = InlineKeyboardMarkup([first_row, second_row, claude_buttons, back_button])
    
    try:
        await query.edit_message_text(text=details_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except telegram.error.BadRequest as e:
        if "Message is not modified" in str(e):
            pass
#for the settings menu
async def model_settings_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = query.from_user.id

    if data == 'model-ai_model':
        current_model = db.get_user_attribute(user_id, "current_model")
        text = f"{config.models['info'][current_model]['description']}\n\n"

        score_dict = config.models["info"][current_model]["scores"]
        for score_key, score_value in score_dict.items():
            text += f"{'🟢' * score_value}{'⚪️' * (5 - score_value)} – {score_key}\n"

        text += "\nSelect <b>model</b>:\n"
        
        buttons = []
        claude_buttons = []
        other_buttons = []

        for model_key in config.models["available_text_models"]:
            title = config.models["info"][model_key]["name"]
            if model_key == current_model:
                title = "✅ " + title
            
            # Adjust callback data to include a prefix for Claude models
            if "claude" in model_key.lower():
                callback_data = f"claude-model-set_settings|{model_key}"
                claude_buttons.append(InlineKeyboardButton(title, callback_data=callback_data))
            else:
                callback_data = f"model-set_settings|{model_key}"
                other_buttons.append(InlineKeyboardButton(title, callback_data=callback_data))

        half_size = len(other_buttons) // 2
        first_row = other_buttons[:half_size]
        second_row = other_buttons[half_size:]
        back_button = [InlineKeyboardButton("⬅️", callback_data='model-back_to_settings')]
        
        reply_markup = InlineKeyboardMarkup([first_row, second_row, claude_buttons, back_button])

        await query.edit_message_text(text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

    elif data.startswith('claude-model-set_settings|'):
        # Check for API key
        if config.anthropic_api_key is None or config.anthropic_api_key == "":
            await context.bot.send_message(
                chat_id=user_id,
                text="This bot does not have the Anthropic models available :(",
                parse_mode='Markdown'
            )
            return
        # Continue handling setting the model as usual
        _, model_key = data.split("|")
        db.set_user_attribute(user_id, "current_model", model_key)
        await display_model_info(query, user_id, context)

    elif data.startswith('model-set_settings|'):
        _, model_key = data.split("|")
        # Prevent Claude models from being set without API key
        if "claude" in model_key.lower() and (config.anthropic_api_key is None or config.anthropic_api_key == ""):
            await context.bot.send_message(
                chat_id=user_id,
                text="This bot does not have the Anthropic models available :(",
                parse_mode='Markdown'
            )
            return
        db.set_user_attribute(user_id, "current_model", model_key)
        await display_model_info(query, user_id, context)

    elif data.startswith('model-artist-set_model|'):
        _, model_key = data.split("|")
        await switch_between_artist_handler(query, user_id, model_key)

    elif data == 'model-artist_model':
        await artist_model_settings_handler(query, user_id)

    elif data.startswith('model-artist-set_model|'):
        # Extract the model key and set it in the preferences
        _, model_key = data.split("|")
        preferences = db.get_user_attribute(user_id, "image_preferences")
        preferences["model"] = model_key
        db.set_user_attribute(user_id, "image_preferences", preferences)
        await artist_model_settings_handler(query, user_id)

    elif data.startswith("model-artist-set_images|"):
        _, n_images = data.split("|")
        preferences = db.get_user_attribute(user_id, "image_preferences")
        preferences["n_images"] = int(n_images)
        db.set_user_attribute(user_id, "image_preferences", preferences)
        await artist_model_settings_handler(query, user_id)

    elif data.startswith("model-artist-set_resolution|"):
        _, resolution = data.split("|")
        preferences = db.get_user_attribute(user_id, "image_preferences")
        preferences["resolution"] = resolution
        db.set_user_attribute(user_id, "image_preferences", preferences)
        await artist_model_settings_handler(query, user_id)

    elif data.startswith("model-artist-set_quality|"):
        _, quality = data.split("|")
        preferences = db.get_user_attribute(user_id, "image_preferences")
        preferences["quality"] = quality
        db.set_user_attribute(user_id, "image_preferences", preferences)
        await artist_model_settings_handler(query, user_id)

    elif data == 'model-back_to_settings':
        text, reply_markup = get_settings_menu(user_id)  # pass user_id correctly
        await query.edit_message_text(text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

async def artist_model_settings_handler(query, user_id):
    """Display artist model selection settings."""
    current_preferences = db.get_user_attribute(user_id, "image_preferences")
    current_model = current_preferences.get("model", "dalle-2")
    
    model_info = config.models["info"][current_model]
    description = model_info["description"]
    scores = model_info["scores"]

    # Build the details text with the description and scores
    details_text = f"{description}\n\n"
    for score_key, score_value in scores.items():
        details_text += f"{'🟢' * score_value}{'⚪️' * (5 - score_value)} – {score_key}\n"

    # Create buttons for available image models
    buttons = []
    for model_key in config.models["available_image_models"]:
        title = config.models["info"][model_key]["name"]
        if model_key == current_model:
            title = "✅ " + title
        buttons.append(InlineKeyboardButton(title, callback_data=f"model-artist-set_model|{model_key}"))
    
    # Add model-specific configurations
    if current_model == "dalle-2":
        details_text += "\nFor this model, choose the number of images to generate and the resolution:"
        # Add checkmarked buttons for the number of images
        n_images = current_preferences.get("n_images", 1)
        images_buttons = [
            InlineKeyboardButton(
                                 f"✅ {i} image" if i == n_images and i == 1 else f"✅ {i} images" if i == n_images else f"{i} image" if i == 1 else f"{i} images",
                                 callback_data=f"model-artist-set_images|{i}")
            for i in range(1, 4)
        ]
        # Add checkmarked buttons for the resolution
        current_resolution = current_preferences.get("resolution", "1024x1024")
        resolution_buttons = [
            InlineKeyboardButton(f"✅ {res_key}" if res_key == current_resolution else f"{res_key}",
                                 callback_data=f"model-artist-set_resolution|{res_key}")
            for res_key in config.models["info"]["dalle-2"]["resolutions"].keys()
        ]
        keyboard = [buttons] + [images_buttons] + [resolution_buttons]

    elif current_model == "dalle-3":
        details_text += "\nFor this model, choose the quality of the images and the resolution:"
        # Add checkmarked buttons for quality levels
        current_quality = current_preferences.get("quality", "standard")
        quality_buttons = [
            InlineKeyboardButton(f"✅ {quality_key}" if quality_key == current_quality else f"{quality_key}",
                                 callback_data=f"model-artist-set_quality|{quality_key}")
            for quality_key in config.models["info"]["dalle-3"]["qualities"].keys()
        ]
        # Add checkmarked buttons for resolution based on selected quality
        current_resolution = current_preferences.get("resolution", "1024x1024")
        resolution_buttons = [
            InlineKeyboardButton(f"✅ {res_key}" if res_key == current_resolution else f"{res_key}",
                                 callback_data=f"model-artist-set_resolution|{res_key}")
            for res_key in config.models["info"]["dalle-3"]["qualities"][current_quality]["resolutions"].keys()
        ]
        keyboard = [buttons] + [quality_buttons] + [resolution_buttons]
    else:
        keyboard = [buttons]

    # Add back button
    keyboard.append([InlineKeyboardButton("⬅️", callback_data='model-back_to_settings')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(text=details_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except telegram.error.BadRequest as e:
        if "Message is not modified" in str(e):
            pass

#is needed to make sure the api call isnt made with wrong parameters
async def switch_between_artist_handler(query, user_id, model_key):
    """Handle artist model selection and update preferences."""
    preferences = db.get_user_attribute(user_id, "image_preferences")
    
    # Update the model and set other values based on the chosen model
    preferences["model"] = model_key
    if model_key == "dalle-2":
        preferences["quality"] = "standard"
    elif model_key == "dalle-3":
        preferences["n_images"] = 1
    # Set the default resolution to 1024x1024 when switching models
    preferences["resolution"] = "1024x1024"
    
    # Save the updated preferences back to the database
    db.set_user_attribute(user_id, "image_preferences", preferences)
    await artist_model_settings_handler(query, user_id)

#name this show_balance_handle and change the name of the other one if you want all the details shown in one place
async def show_balance_handle_full_details(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    current_token_balance = db.check_token_balance(user_id)
    current_euro_balance = db.get_user_euro_balance(user_id)
    
    # count total usage statistics
    total_n_spent_dollars = 0
    total_n_used_tokens = 0
    financials = db.get_user_financials(user_id)
    total_topup = financials['total_topup']
    total_donated = financials['total_donated']

    n_used_tokens_dict = db.get_user_attribute(user_id, "n_used_tokens")
    n_generated_images = db.get_user_attribute(user_id, "n_generated_images")
    n_transcribed_seconds = db.get_user_attribute(user_id, "n_transcribed_seconds")

    details_text = "🏷️ Details:\n"
    for model_key in sorted(n_used_tokens_dict.keys()):
        n_input_tokens, n_output_tokens = n_used_tokens_dict[model_key]["n_input_tokens"], n_used_tokens_dict[model_key]["n_output_tokens"]
        total_n_used_tokens += n_input_tokens + n_output_tokens

        n_input_spent_dollars = config.models["info"][model_key]["price_per_1000_input_tokens"] * (n_input_tokens / 1000)
        n_output_spent_dollars = config.models["info"][model_key]["price_per_1000_output_tokens"] * (n_output_tokens / 1000)
        total_n_spent_dollars += n_input_spent_dollars + n_output_spent_dollars

        details_text += f"- {model_key}: <b>{n_input_spent_dollars + n_output_spent_dollars:.03f}$</b> / <b>{n_input_tokens + n_output_tokens} tokens</b>\n"

    # image generation
    image_generation_n_spent_dollars = config.models["info"]["dalle-2"]["price_per_1_image"] * n_generated_images
    if n_generated_images != 0:
        details_text += f"- DALL·E 2 (image generation): <b>{image_generation_n_spent_dollars:.03f}$</b> / <b>{n_generated_images} generated images</b>\n"

    total_n_spent_dollars += image_generation_n_spent_dollars

    # voice recognition
    voice_recognition_n_spent_dollars = config.models["info"]["whisper"]["price_per_1_min"] * (n_transcribed_seconds / 60)
    if n_transcribed_seconds != 0:
        details_text += f"- Whisper (voice recognition): <b>{voice_recognition_n_spent_dollars:.03f}$</b> / <b>{n_transcribed_seconds:.01f} seconds</b>\n"

    total_n_spent_dollars += voice_recognition_n_spent_dollars

    text = f"Your euro balance is <b>€{current_euro_balance}</b> \n\n"
    text += "You:\n\n"
    text += f"   Have yet to make your first payment 😢\n" if total_topup == 0 else f"   Paid <b>{total_topup:.02f}€</b> ❤️\n" if total_topup < 30 else f"   Paid <b>{total_topup:.02f}€</b>. I'm glad you really like using the bot!❤️\n"
    text += f"   Have not made any donations.\n\n" if total_donated == 0 else f"   Donated <b>{total_donated:.02f}€</b>. You're a legend! ❤️\n\n" if total_donated < 10 else f"   \nDonated <b>{total_donated:.02f}€</b>. I appreciate your continued support!! ❤️❤️\n\n"
    text += f"   Spent ≈ <b>{total_n_spent_dollars:.03f}$</b> 💵\n"
    text += f"   Used <b>{total_n_used_tokens}</b> tokens 🪙\n\n"
    text += details_text

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def show_balance_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    current_token_balance = db.check_token_balance(user_id) #if you use token balance
    current_euro_balance = db.get_user_euro_balance(user_id)

    text = f"Your euro balance is <b>€{current_euro_balance:.2f}</b> 💶\n\n"
    text += "Press 'Details' for more information.\n"

    keyboard = [
        [InlineKeyboardButton("🏷️ Details", callback_data='show_details')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

async def callback_show_details_old(update: Update, context: CallbackContext):
    print("Details button pressed")
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    current_euro_balance = db.get_user_euro_balance(user_id)

    # Fetch usage statistics
    n_used_tokens_dict = db.get_user_attribute(user_id, "n_used_tokens")
    n_generated_images = db.get_user_attribute(user_id, "n_generated_images")
    n_transcribed_seconds = db.get_user_attribute(user_id, "n_transcribed_seconds")
    financials = db.get_user_financials(user_id)
    total_topup = financials['total_topup']
    total_donated = financials['total_donated']
    
    
    details_text = "🏷️ Details:\n"
    total_n_spent_dollars = 0
    total_n_used_tokens = 0
    
    for model_key in sorted(n_used_tokens_dict.keys()):
        n_input_tokens, n_output_tokens = n_used_tokens_dict[model_key]["n_input_tokens"], n_used_tokens_dict[model_key]["n_output_tokens"]
        total_n_used_tokens += n_input_tokens + n_output_tokens

        n_input_spent_dollars = config.models["info"][model_key]["price_per_1000_input_tokens"] * (n_input_tokens / 1000)
        n_output_spent_dollars = config.models["info"][model_key]["price_per_1000_output_tokens"] * (n_output_tokens / 1000)
        total_n_spent_dollars += n_input_spent_dollars + n_output_spent_dollars

        details_text += f"- {model_key}: <b>{n_input_spent_dollars + n_output_spent_dollars:.03f}$</b> / <b>{n_input_tokens + n_output_tokens} tokens</b>\n"

    # image generation and voice recognition calculations, similar to the initial function
    image_generation_n_spent_dollars = config.models["info"]["dalle-2"]["price_per_1_image"] * n_generated_images
    voice_recognition_n_spent_dollars = config.models["info"]["whisper"]["price_per_1_min"] * (n_transcribed_seconds / 60)

    total_n_spent_dollars += image_generation_n_spent_dollars + voice_recognition_n_spent_dollars

    details_text += f"- DALL·E 2 (image generation): <b>{image_generation_n_spent_dollars:.03f}$</b> / <b>{n_generated_images} images</b>\n"
    details_text += f"- Whisper (voice recognition): <b>{voice_recognition_n_spent_dollars:.03f}$</b> / <b>{n_transcribed_seconds:.01f} seconds</b>\n"

    text = f"Your euro balance is <b>€{current_euro_balance:.3f}</b> 💶\n\n"
    text += "You:\n\n"
    text += f"   Have yet to make your first payment 😢\n" if total_topup == 0 else f"   Paid <b>{total_topup:.02f}€</b> ❤️\n" if total_topup < 30 else f"   Paid <b>{total_topup:.02f}€</b>. I'm glad you really like using the bot!❤️\n"
    text += f"   Have not made any donations.\n\n" if total_donated == 0 else f"   Donated <b>{total_donated:.02f}€</b>. You're a legend! ❤️\n\n" if total_donated < 10 else f"   \nDonated <b>{total_donated:.02f}€</b>!. I appreciate your continued support!! ❤️❤️\n\n"
    text += f"   Spent ≈ <b>{total_n_spent_dollars:.03f}$</b> 💵\n"
    text += f"   Used <b>{total_n_used_tokens}</b> tokens 🪙\n\n"
    text += details_text

    print("Attempting to edit message")
    try:
        await query.edit_message_text(text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"Failed to edit message: {e}")
    print("Message edit attempted")

# Initialize "total_spent" field for all existing users in the database
def initialize_total_spent_field():
    all_users = db.user_collection.find()
    for user in all_users:
        if "total_spent" not in user:
            db.user_collection.update_one(
                {"_id": user["_id"]},
                {"$set": {"total_spent": 0}}
            )

async def callback_show_details(update: Update, context: CallbackContext):
    print("Details button pressed")
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    initialize_total_spent_field()
    # Initialize missing fields for DALL-E 2 and DALL-E 3 tracking
    default_dalle_2 = {"images": 0, "cost": 0.0}
    default_dalle_3 = {"images": 0, "cost": 0.0}

    all_users = db.user_collection.find()
    for user in all_users:
        if "dalle_2" not in user or user["dalle_2"] is None:
            db.user_collection.update_one(
                {"_id": user["_id"]},
                {"$set": {"dalle_2": default_dalle_2}}
            )
        if "dalle_3" not in user or user["dalle_3"] is None:
            db.user_collection.update_one(
                {"_id": user["_id"]},
                {"$set": {"dalle_3": default_dalle_3}}
            )

    # Fetch current balance and stats after ensuring fields exist
    current_euro_balance = db.get_user_euro_balance(user_id)
    n_used_tokens_dict = db.get_user_attribute(user_id, "n_used_tokens")
    n_generated_images = db.get_user_attribute(user_id, "n_generated_images")
    n_transcribed_seconds = db.get_user_attribute(user_id, "n_transcribed_seconds")
    financials = db.get_user_financials(user_id)
    total_topup = financials['total_topup']
    total_donated = financials['total_donated']
    total_spent = db.get_user_attribute(user_id, "total_spent")

    # Retrieve DALL-E 2 and DALL-E 3 data
    dalle_2_data = db.get_user_attribute(user_id, "dalle_2") or default_dalle_2
    dalle_3_data = db.get_user_attribute(user_id, "dalle_3") or default_dalle_3

    details_text = "🏷️ Details:\n"
    total_n_spent_dollars = 0
    total_n_used_tokens = 0

    # Calculate the total spent for each model
    for model_key in sorted(n_used_tokens_dict.keys()):
        n_input_tokens, n_output_tokens = n_used_tokens_dict[model_key]["n_input_tokens"], n_used_tokens_dict[model_key]["n_output_tokens"]
        total_n_used_tokens += n_input_tokens + n_output_tokens

        n_input_spent_dollars = config.models["info"][model_key]["price_per_1000_input_tokens"] * (n_input_tokens / 1000)
        n_output_spent_dollars = config.models["info"][model_key]["price_per_1000_output_tokens"] * (n_output_tokens / 1000)
        total_n_spent_dollars += n_input_spent_dollars + n_output_spent_dollars

        details_text += f"- {model_key}: <b>{n_input_spent_dollars + n_output_spent_dollars:.03f}€</b> / <b>{n_input_tokens + n_output_tokens} tokens</b>\n"

    # Add DALL-E 2 and DALL-E 3 usage to the details
    details_text += f"- DALL·E 2 (image generation): <b>{dalle_2_data['cost']:.03f}€</b> / <b>{dalle_2_data['images']} images</b>\n"
    details_text += f"- DALL·E 3 (image generation): <b>{dalle_3_data['cost']:.03f}€</b> / <b>{dalle_3_data['images']} images</b>\n"

    # Add Whisper usage
    voice_recognition_n_spent_dollars = config.models["info"]["whisper"]["price_per_1_min"] * (n_transcribed_seconds / 60)
    total_n_spent_dollars += voice_recognition_n_spent_dollars

    details_text += f"- Whisper (voice recognition): <b>{voice_recognition_n_spent_dollars:.03f}€</b> / <b>{n_transcribed_seconds:.01f} seconds</b>\n"

    # Summary information
    text = f"Your euro balance is <b>€{current_euro_balance:.3f}</b> 💶\n\n"
    text += "You:\n\n"
    text += f"   Have yet to make your first payment 😢\n" if total_topup == 0 else f"   Paid <b>{total_topup:.02f}€</b> ❤️\n" if total_topup < 30 else f"   Paid <b>{total_topup:.02f}€</b>. I'm glad you really like using the bot!❤️\n"
    text += f"   Have not made any donations.\n\n" if total_donated == 0 else f"   Donated <b>{total_donated:.02f}€</b>. You're a legend! ❤️\n\n" if total_donated < 10 else f"   \nDonated <b>{total_donated:.02f}€</b>!. I appreciate your continued support!! ❤️❤️\n\n"
    text += f"   Spent ≈ <b>{total_spent:.03f}€</b> 💵\n"
    text += f"   Used <b>{total_n_used_tokens}</b> tokens 🪙\n\n"
    text += details_text

    print("Attempting to edit message")
    try:
        await query.edit_message_text(text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"Failed to edit message: {e}")
    print("Message edit attempted")

async def edited_message_handle(update: Update, context: CallbackContext):

    


    if update.edited_message.chat.type == "private":
        text = "🥲 Unfortunately, message <b>editing</b> is not supported"
        await update.edited_message.reply_text(text, parse_mode=ParseMode.HTML)

#if you want to give the full error to all users, change this to error_handle
async def error_handle_noadmincheck(update: Update, context: CallbackContext) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    try:
        # collect error message
        tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
        tb_string = "".join(tb_list)
        update_str = update.to_dict() if isinstance(update, Update) else str(update)
        message = (
            f"An exception was raised while handling an update\n"
            f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
            "</pre>\n\n"
            f"<pre>{html.escape(tb_string)}</pre>"
        )

        # split text into multiple messages due to 4096 character limit
        for message_chunk in split_text_into_chunks(message, 4096):
            try:
                await context.bot.send_message(update.effective_chat.id, message_chunk, parse_mode=ParseMode.HTML)
            except telegram.error.BadRequest:
                # answer has invalid characters, so we send it without parse_mode
                await context.bot.send_message(update.effective_chat.id, message_chunk)
    except:
        await context.bot.send_message(update.effective_chat.id, "Some error in error handler")

async def error_handle(update: Update, context: CallbackContext) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    # Check if the update has an associated user ID
    user_id = None
    if update and update.effective_user:
        user_id = update.effective_user.id

    admin_ids = config.roles.get('admin', [])
    is_admin = user_id in admin_ids
    developer = config.developer_username

    try:
        # Collect the error message
        tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
        tb_string = "".join(tb_list)
        update_str = update.to_dict() if isinstance(update, Update) else str(update)
        message = (
            f"An exception was raised while handling an update\n"
            f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
            "</pre>\n\n"
            f"<pre>{html.escape(tb_string)}</pre>"
        )

        # Send a full error message if the user is an admin, otherwise send a generic message
        if is_admin:
            # Split text into multiple messages due to 4096 character limit
            for message_chunk in split_text_into_chunks(message, 4096):
                try:
                    await context.bot.send_message(update.effective_chat.id, message_chunk, parse_mode=ParseMode.HTML)
                except telegram.error.BadRequest:
                    # Answer has invalid characters, so we send it without parse_mode
                    await context.bot.send_message(update.effective_chat.id, message_chunk)
        else:
            error_for_user = (
                f"An unexpected error occurred. "
                f"{'Please try again, or contact ' + ', '.join(developer) + ' if the issue persists.' if developer else 'Please try again or contact the support if the issue persists.'} \n\n"
            )

            await context.bot.send_message(
                update.effective_chat.id,
                #"An unexpected error occurred. Please try again or contact the developer if the issue persists."
                error_for_user
            )
    except Exception as handler_error:
        logger.error("Error in error handler: %s", handler_error)
        await context.bot.send_message(update.effective_chat.id, "Some error in error handler")

#set bot commands
async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("/new", "Start new dialog 🆕"),
        BotCommand("/retry", "Re-generate response for previous query 🔁"),
        BotCommand("/mode", "Select chat mode 🎭"),
        BotCommand("/balance", "Show balance 💰"),
        BotCommand("/topup", "Top-up your balance 💳"), 
        BotCommand("/settings", "Show settings ⚙️"),
        BotCommand("/help", "Show help message ❓"),
        BotCommand("/role", "Show your role 🎫"),
        BotCommand("/model", "Show your current selected model 🔍"),
         
    ])

bot_instance = None

def run_bot() -> None:

    thread = threading.Thread(target=start_asyncio_loop, daemon=True)
    thread.start()

    global bot_instance
    application = ApplicationBuilder().token(config.telegram_token).build()
    bot_instance = application.bot

    update_user_roles_from_config(db, config.roles)
    configure_logging()

    application = (
        ApplicationBuilder()
        .token(config.telegram_token)
        .concurrent_updates(True)
        .rate_limiter(AIORateLimiter(max_retries=5))
        .http_version("1.1")
        .get_updates_http_version("1.1")
        .post_init(post_init)
        .build()
    )

    # add handlers
    user_filter = filters.ALL
    if len(config.allowed_telegram_usernames) > 0:
        usernames = [x for x in config.allowed_telegram_usernames if isinstance(x, str)]
        any_ids = [x for x in config.allowed_telegram_usernames if isinstance(x, int)]
        user_ids = [x for x in any_ids if x > 0]
        group_ids = [x for x in any_ids if x < 0]
        user_filter = filters.User(username=usernames) | filters.User(user_id=user_ids) | filters.Chat(chat_id=group_ids)

    application.add_handler(CommandHandler("start", start_handle, filters=user_filter))
    application.add_handler(CommandHandler("help", help_handle, filters=user_filter))
    application.add_handler(CommandHandler("help_group_chat", help_group_chat_handle, filters=user_filter))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, message_handle))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND & user_filter, message_handle))
    application.add_handler(MessageHandler(filters.VIDEO & ~filters.COMMAND & user_filter, unsupport_message_handle))
    application.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND & user_filter, unsupport_message_handle))
    application.add_handler(CommandHandler("retry", retry_handle, filters=user_filter))
    application.add_handler(CommandHandler("new", new_dialog_handle, filters=user_filter))
    application.add_handler(CommandHandler("cancel", cancel_handle, filters=user_filter))

    application.add_handler(MessageHandler(filters.VOICE & user_filter, voice_message_handle))

    application.add_handler(CommandHandler("mode", show_chat_modes_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(show_chat_modes_callback_handle, pattern="^show_chat_modes"))
    application.add_handler(CallbackQueryHandler(set_chat_mode_handle, pattern="^set_chat_mode"))

    application.add_handler(CommandHandler("settings", settings_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(set_settings_handle, pattern="^set_settings"))
    application.add_handler(CallbackQueryHandler(model_settings_handler, pattern='^model-'))
    application.add_handler(CallbackQueryHandler(model_settings_handler, pattern='^claude-model-'))

    application.add_handler(CommandHandler("balance", show_balance_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(callback_show_details, pattern='^show_details$'))
    #custom commands
    application.add_handler(CommandHandler('role', show_user_role))
    application.add_handler(CommandHandler('model', show_user_model))
    application.add_handler(CommandHandler('token_balance', token_balance_command))
    application.add_handler(CommandHandler("topup", topup_handle, filters=filters.ALL))
    application.add_handler(CallbackQueryHandler(topup_callback_handle, pattern='^topup\\|'))

    #admin commands
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler('get_user_count', get_user_count))
    application.add_handler(CommandHandler('list_user_roles', list_user_roles))
    application.add_handler(CommandHandler('message_id', send_message_to_id))
    application.add_handler(CommandHandler('message_username', send_message_to_username))
    application.add_handler(CommandHandler('message_name', send_message_to_name))
    application.add_handler(CommandHandler('message_role', send_message_to_role))
    application.add_handler(CommandHandler('message_all', send_message_to_all))
    application.add_handler(CommandHandler('change_role', change_role))
    application.add_handler(CallbackQueryHandler(handle_role_change, pattern='^set_role\\|'))

    application.add_error_handler(error_handle)

    # start the bot
    application.run_polling()


if __name__ == "__main__":

    run_bot()

