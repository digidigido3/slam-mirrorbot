import os
import time
import html
import asyncio
import aiohttp
import json
import feedparser
import requests

from telegram.ext import CommandHandler
from telegram import ParseMode

from urllib.parse import quote as urlencode, urlsplit

from pyrogram import Client, filters, emoji
from pyrogram.parser import html as pyrogram_html
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.handlers import MessageHandler, CallbackQueryHandler

from bot import app, dispatcher, IMAGE_URL
from bot.helper import custom_filters
from bot.helper.telegram_helper.filters import CustomFilters

search_lock = asyncio.Lock()
search_info = {False: dict(), True: dict()}

async def return_search(query, page=1, sukebei=False):
    page -= 1
    query = query.lower().strip()
    used_search_info = search_info[sukebei]
    async with search_lock:
        results, get_time = used_search_info.get(query, (None, 0))
        if (time.time() - get_time) > 3600:
            results = []
            async with aiohttp.ClientSession() as session:
                async with session.get(f'https://{"sukebei." if sukebei else ""}nyaa.si/?page=rss&q={urlencode(query)}') as resp:
                    d = feedparser.parse(await resp.text())
            text = ''
            a = 0
            parser = pyrogram_html.HTML(None)
            for i in sorted(d['entries'], key=lambda i: int(i['nyaa_seeders']), reverse=True):
                if i['nyaa_size'].startswith('0'):
                    continue
                if not int(i['nyaa_seeders']):
                    break
                link = i['link']
                splitted = urlsplit(link)
                if splitted.scheme == 'magnet' and splitted.query:
                    link = f'<code>{link}</code>'
                newtext = f'''<b>{a + 1}.</b> <code>{html.escape(i["title"])}</code>
<b>Link:</b> <code>{link}</code>
<b>Size:</b> <code>{i["nyaa_size"]}</code>
<b>Seeders:</b> <code>{i["nyaa_seeders"]}</code>
<b>Leechers:</b> <code>{i["nyaa_leechers"]}</code>
<b>Category:</b> <code>{i["nyaa_category"]}</code>\n\n'''
                futtext = text + newtext
                if (a and not a % 10) or len((await parser.parse(futtext))['message']) > 4096:
                    results.append(text)
                    futtext = newtext
                text = futtext
                a += 1
            results.append(text)
        ttl = time.time()
        used_search_info[query] = results, ttl
        try:
            return results[page], len(results), ttl
        except IndexError:
            return '', len(results), ttl

message_info = dict()
ignore = set()

@app.on_message(filters.command(['nyaa']))
async def nyaa_search(client, message):
    text = message.text.split(' ')
    text.pop(0)
    query = ' '.join(text)
    await init_search(client, message, query, False)

@app.on_message(filters.command(['sukebei']))
async def nyaa_search_sukebei(client, message):
    text = message.text.split(' ')
    text.pop(0)
    query = ' '.join(text)
    await init_search(client, message, query, True)

async def init_search(client, message, query, sukebei):
    result, pages, ttl = await return_search(query, sukebei=sukebei)
    if not result:
        await message.reply_text('No results found')
    else:
        buttons = [InlineKeyboardButton(f'1/{pages}', 'nyaa_nop'), InlineKeyboardButton(f'Next', 'nyaa_next')]
        if pages == 1:
            buttons.pop()
        reply = await message.reply_text(result, reply_markup=InlineKeyboardMarkup([
            buttons 
        ]))
        message_info[(reply.chat.id, reply.message_id)] = message.from_user.id, ttl, query, 1, pages, sukebei

@app.on_callback_query(custom_filters.callback_data('nyaa_nop'))
async def nyaa_nop(client, callback_query):
    await callback_query.answer(cache_time=3600)

callback_lock = asyncio.Lock()
@app.on_callback_query(custom_filters.callback_data(['nyaa_back', 'nyaa_next']))
async def nyaa_callback(client, callback_query):
    message = callback_query.message
    message_identifier = (message.chat.id, message.message_id)
    data = callback_query.data
    async with callback_lock:
        if message_identifier in ignore:
            await callback_query.answer()
            return
        user_id, ttl, query, current_page, pages, sukebei = message_info.get(message_identifier, (None, 0, None, 0, 0, None))
        og_current_page = current_page
        if data == 'nyaa_back':
            current_page -= 1
        elif data == 'nyaa_next':
            current_page += 1
        if current_page < 1:
            current_page = 1
        elif current_page > pages:
            current_page = pages
        ttl_ended = (time.time() - ttl) > 3600
        if ttl_ended:
            text = getattr(message.text, 'html', 'Search expired')
        else:
            if callback_query.from_user.id != user_id:
                await callback_query.answer('...no', cache_time=3600)
                return
            text, pages, ttl = await return_search(query, current_page, sukebei)
        buttons = [InlineKeyboardButton(f'Prev', 'nyaa_back'), InlineKeyboardButton(f'{current_page}/{pages}', 'nyaa_nop'), InlineKeyboardButton(f'Next', 'nyaa_next')]
        if ttl_ended:
            buttons = [InlineKeyboardButton('Search Expired', 'nyaa_nop')]
        else:
            if current_page == 1:
                buttons.pop(0)
            if current_page == pages:
                buttons.pop()
        if ttl_ended or current_page != og_current_page:
            await callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([
                buttons
            ]))
        message_info[message_identifier] = user_id, ttl, query, current_page, pages, sukebei
        if ttl_ended:
            ignore.add(message_identifier)
    await callback_query.answer()

# Using upstream API based on: https://github.com/Ryuk-me/Torrents-Api
# Implemented by https://github.com/jusidama18

class TorrentSearch:
    index = 0
    query = None
    message = None
    response = None
    response_range = None

    RESULT_LIMIT = 4
    RESULT_STR = None

    def __init__(self, command: str, source: str, result_str: str):
        self.command = command
        self.source = source.rstrip('/')
        self.RESULT_STR = result_str

        app.add_handler(MessageHandler(self.find, filters.command([command])))
        app.add_handler(CallbackQueryHandler(self.previous, filters.regex(f"{self.command}_previous")))
        app.add_handler(CallbackQueryHandler(self.delete, filters.regex(f"{self.command}_delete")))
        app.add_handler(CallbackQueryHandler(self.next, filters.regex(f"{self.command}_next")))

    def get_formatted_string(self, values):
        string = self.RESULT_STR.format(**values)
        magnet = values.get('magnet', values.get('Magnet'))  # Avoid updating source dict
        if (magnet):
            string += f"➲Magnet: `{magnet.split('&tr', 1)[0]}`"
        return string

    async def update_message(self):
        prevBtn = InlineKeyboardButton(f"Prev", callback_data=f"{self.command}_previous")
        delBtn = InlineKeyboardButton(f"{emoji.CROSS_MARK}", callback_data="delete")
        nextBtn = InlineKeyboardButton(f"Next", callback_data=f"{self.command}_next")

        inline = []
        if (self.index != 0):
            inline.append(prevBtn)
        inline.append(delBtn)
        if (self.index != len(self.response_range) - 1):
            inline.append(nextBtn)

        result = f"**Page - {self.index+1}**\n\n"
        result += "\n\n═════════════════════════\n\n".join(
            self.get_formatted_string(self.response[self.response_range[self.index]+i])
            for i in range(self.RESULT_LIMIT)
        )

        await self.message.edit(
            result,
            reply_markup=InlineKeyboardMarkup([inline]),
            parse_mode="markdown",
        )

    async def find(self, client, message):
        if len(message.command) < 2:
            await message.reply_text(f"Usage: /{self.command} query")
            return

        query = urlencode(message.text.split(None, 1)[1])
        self.message = await message.reply_text("Searching")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.source}/{query}") \
                        as resp:
                    self.response = await resp.json(content_type=None)
                    self.response_range = range(0, len(self.response), self.RESULT_LIMIT)
        except Exception as exc:
            await self.message.edit(f"No Results Found.")
            return
        await self.update_message()
        await session.close()

    async def delete(self, client, message):
        index = 0
        query = None
        message = None
        response = None
        response_range = None

    async def previous(self, client, message):
        self.index -= 1
        await self.update_message()

    async def next(self, client, message):
        self.index += 1
        await self.update_message()

RESULT_STR_1337 = (
    "➲Name: `{Name}`\n"
    "➲Size: {Size}\n"
    "➲Seeders: {Seeders} || ➲Leechers: {Leechers}\n"
)
RESULT_STR_PIRATEBAY = (
    "➲Name: `{Name}`\n"
    "➲Size: {Size}\n"
    "➲Seeders: {Seeders} || ➲Leechers: {Leechers}\n"
)
RESULT_STR_TGX = (
    "➲Name: `{Name}`\n" 
    "➲Size: {Size}\n"
    "➲Seeders: {Seeders} || ➲Leechers: {Leechers}\n"
)
RESULT_STR_YTS = (
    "➲Name: `{Name}`\n"
    "➲Size: {Size}\n"
    "➲Seeders: {Seeders} || ➲Leechers: {Leechers}\n"
    "➲1st Link: `{Dwnload1}`\n"
    "➲2nd Link: `{Download2}`"
)
RESULT_STR_EZTV = (
    "➲Name: `{Name}`\n"
    "➲Size: {Size}\n"
    "➲Seeders: {Seeders} || ➲Leechers: {Leechers}\n"
)
RESULT_STR_TORLOCK = (
    "➲Name: `{Name}`\n"
    "➲Size: {Size}\n"
    "➲Seeders: {Seeders} || ➲Leechers: {Leechers}\n"
)
RESULT_STR_RARBG = (
    "➲Name: `{Name}`\n"
    "➲Size: {Size}\n"
    "➲Seeders: {Seeders} || ➲Leechers: {Leechers}\n"
)
RESULT_STR_ALL = (
    "➲Name: `{Name}`\n"
    "➲Size: {Size}\n"
    "➲Seeders: {Seeders} || ➲Leechers: {Leechers}\n"
)

torrents_dict = {
    '1337x': {'source': "https://torrenter-api.herokuapp.com/api/1337x/", 'result_str': RESULT_STR_1337},
    'piratebay': {'source': "https://torrenter-api.herokuapp.com/api/piratebay/", 'result_str': RESULT_STR_PIRATEBAY},
    'tgx': {'source': "https://torrenter-api.herokuapp.com/api/tgx/", 'result_str': RESULT_STR_TGX},
    'yts': {'source': "https://torrenter-api.herokuapp.com/api/yts/", 'result_str': RESULT_STR_YTS},
    'eztv': {'source': "https://torrenter-api.herokuapp.com/api/eztv/", 'result_str': RESULT_STR_EZTV},
    'torlock': {'source': "https://torrenter-api.herokuapp.com/api/torlock/", 'result_str': RESULT_STR_TORLOCK},
    'rarbg': {'source': "https://torrenter-api.herokuapp.com/api/rarbg/", 'result_str': RESULT_STR_RARBG},
    'ts': {'source': "https://torrenter-api.herokuapp.com/api/all/", 'result_str': RESULT_STR_ALL}
}

torrent_handlers = []
for command, value in torrents_dict.items():
    torrent_handlers.append(TorrentSearch(command, value['source'], value['result_str']))

def searchhelp(update, context):
    help_string = '''
• /nyaa <i>[search query]</i>
• /sukebei <i>[search query]</i>
• /1337x <i>[search query]</i>
• /piratebay <i>[search query]</i>
• /tgx <i>[search query]</i>
• /yts <i>[search query]</i>
• /eztv <i>[search query]</i>
• /torlock <i>[search query]</i>
• /rarbg <i>[search query]</i>
• /ts <i>[search query]</i>
'''
    update.effective_message.reply_photo(IMAGE_URL, help_string, parse_mode=ParseMode.HTML)
    
    
SEARCHHELP_HANDLER = CommandHandler("tshelp", searchhelp, filters=CustomFilters.authorized_chat | CustomFilters.authorized_user, run_async=True)
dispatcher.add_handler(SEARCHHELP_HANDLER)
