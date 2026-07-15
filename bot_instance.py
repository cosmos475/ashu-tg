"""
bot_instance.py — Single shared TeleBot instance.

This module exists solely to hold one `telebot.TeleBot` instance that both
app.py and the handler modules import. It is never executed as a script
and has no reason to be loaded under more than one module name, so it
cannot suffer the duplicate-instance problem that arises when app.py is
loaded both as '__main__' and as 'app'.
"""

import telebot

import config

bot = telebot.TeleBot(
    token=config.BOT_TOKEN,
    threaded=False,   # We manage our own processing thread; disable telebot's internal threading
    parse_mode="HTML",
)
