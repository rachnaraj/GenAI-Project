import os
import re
import sys

import fire
import torch
from datasets import load_dataset
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from tqdm import tqdm
from transformers import (AutoModelForCausalLM, AutoTokenizer,
                          BitsAndBytesConfig, DataCollatorForLanguageModeling,
                          GenerationConfig, TrainingArguments)
from trl import SFTTrainer

MODEL_ID = "google/gemma-7b-it"

def prompt_template(record, split, with_ocomment, with_inst):
    INST = "Below is an instruction that describes a task. Write a response that "\
            "appropriately completes the request.\n\nGo through the code changes from old "\
            "code to new code and generate an updated code summary."\

    if with_ocomment:
        USER_TEMPLATE = '''<start_of_turn>user\nOld Comment:\n{}\nNew Code:\n{}\n<end_of_turn>\n'''.\
                        format(record["src_javadoc"], record["dst_method"])

    elif with_ocomment and with_inst:
        USER_TEMPLATE = '''<start_of_turn>user\n{}\n\nOld Comment:\n{}\nNew Code:\n{}\n<end_of_turn>\n'''.\
                        format(INST, record["src_javadoc"], record["dst_method"])

    else:
        USER_TEMPLATE = '''<start_of_turn>user\nCode:\n{}\n<end_of_turn>\n'''.\
                        format(record["dst_method"])

    MODEL_TEMPLATE = '''<start_of_turn>model\nTarget Comment:\n{}<end_of_turn>'''.\
                     format(record["dst_javadoc"])

    if split == "test":
        prompt_template = USER_TEMPLATE
    else:
        prompt_template = (USER_TEMPLATE + MODEL_TEMPLATE)

    return prompt_template


def training(train_ds, valid_ds):

    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID,
                                              device_map="auto",
                                              use_fast=True)
    train_tokenized_inputs = train_ds.map(
        lambda example: tokenizer(example["prompt"],
                                  return_tensors="pt",
                                  truncation=True,
                                  padding=True),
        batched=True
    )
    valid_tokenized_inputs = valid_ds.map(
        lambda example: tokenizer(example["prompt"],
                                  return_tensors="pt",
                                  truncation=True,
                                  padding=True),
        batched=True
    )

    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16
    )

    model = AutoModelForCausalLM.from_pretrained(MODEL_ID,
                                                 device_map="auto",
                                                 torch_dtype=torch.bfloat16,
                                                 quantization_config=bnb_config,
                                                 trust_remote_code=False,
                                                 return_dict=True,
                                                 revision="main")
    model.train()
    model.gradient_checkpointing_enable()
    model = prepare_model_for_kbit_training(model)

    modules = ['q_proj','k_proj','v_proj','o_proj','down_proj', 'up_proj', 'gate_proj']
    peft_config = LoraConfig(
        r=8,
        lora_alpha=32,
        target_modules=modules,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM"
    )

    model = get_peft_model(model, peft_config)
    trainable, total = model.get_nb_trainable_parameters()
    print(f"Trainable: {trainable} | total: {total} | Percentage: {trainable/total*100:.4f}%")

    tokenizer.pad_token = tokenizer.eos_token
    data_collator = DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False)
    torch.cuda.empty_cache()

    training_args = TrainingArguments(
        output_dir="./gemma-7b-it-ft",
        learning_rate=2e-4,
        per_device_train_batch_size=4,
        per_device_eval_batch_size=4,
        max_steps=1,
        logging_strategy="epoch",
        evaluation_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        gradient_accumulation_steps=2,
        fp16=True,
        optim="paged_adamw_8bit"
    )

    trainer = SFTTrainer(
        model=model,
        train_dataset=train_tokenized_inputs,
        eval_dataset=train_tokenized_inputs, # replace this with valid_tokenized_input
        args=training_args,
        peft_config=peft_config,
        dataset_text_field="prompt",
        data_collator=data_collator
    )

    model.config.use_cache=False
    trainer.train()

    return model, tokenizer


def inference(test_ds, tokenizer, model, max_new_tokens):
    generation_config = GenerationConfig(
        temperature=0.1,
        do_sample=True
    )

    model.to("cuda:0")
    model.eval()
    generated_comment = []
    for record in tqdm(test_ds):
        encoding = tokenizer(record["prompt"],
                            return_tensors="pt",
                            add_special_tokens=True)
        encoding = encoding.to("cuda")
        generated_ids = model.generate(**encoding,
                                        max_new_tokens=max_new_tokens,
                                        generation_config = generation_config,
                                        do_sample=True,
                                        pad_token_id=tokenizer.eos_token_id)
        generated_ids = generated_ids[:, encoding.input_ids.shape[1]:]
        generated_text = tokenizer.batch_decode(generated_ids, skip_special_tokens=True)

        generated_comment.append(generated_text)

    return generated_comment

def run(data_dir: str, with_ocomment: bool = False, with_inst: bool = False,
        max_new_tokens: int = 128):

    data_files = {
        "train": "dummy_train.csv",
        "valid": "dummy_train.csv",
        "test": "dummy_train.csv"
    }

    dataset = load_dataset("csv", data_dir=data_dir, data_files=data_files)

    for split in ["train", "valid", "test"]:
        prompt_col = []
        for record in tqdm(dataset[split]):
                prompt_col.append(prompt_template(record, split, with_ocomment, with_inst))

        dataset[split] = dataset[split].add_column("prompt", prompt_col)

    model, tokenizer = training(dataset["train"], dataset["valid"])
    output_comments = inference(dataset["test"], tokenizer, model, max_new_tokens)

    print(output_comments)

if __name__ == "__main__":
    fire.Fire(run)
