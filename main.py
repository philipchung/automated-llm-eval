import pandas as pd
from tqdm import tqdm
from langchain.prompts.prompt import PromptTemplate
import re
import numpy as np
import matplotlib.pyplot as plt

from get_questions import get_questions
from create_chat_completion import create_chat_completion
from prompts import *
from model_performance import model_performance
from private_key import *
from test import run_test
from model_analysis import analysis
from visualize import *

questions =get_questions()

openai_token = key["open-ai"]

engine_options = ["gpt-3.5-turbo", "gpt-4"]
judge_options = ["gpt-3.5-turbo", "gpt-4"]

run_test(engine_options, judge_options)

for engine in engine_options:
    for engine_judge in judge_options:
        analysis(engine, engine_judge, engine+ ' + ' +engine_judge)

create_plots(engine_options, judge_options)