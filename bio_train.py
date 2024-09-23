import argparse
import os.path
import time
from collate.collator import *

from preprocess import *
from transformers import AutoModel, AutoTokenizer, AutoConfig

from models.model import DocREModel
from torch.utils.data import *
from torch.optim import AdamW
from torch.optim.lr_scheduler import get_cosine_schedule_with_warmup
from tqdm import tqdm
from augmentation_graph import augmentation

def set_seed(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)

def train(args, model, train_features, dev_features, test_features):

    def logging(s, print_=True, log_=True):
        if print_:
            print(s)
        if log_:
            with open(args.log_dir, 'a+') as f_log:
                f_log.write(s + '\n')


    def finetune(features, optimizer, num_epoch, num_steps):
        best_score = -1
        train_dataloader = DataLoader(
            features, 
            batch_size=args.train_batch_size, 
            shuffle=True, 
            collate_fn=collate_fn,
            drop_last=True
        )

        train_iterator = range(int(num_epoch))
        total_steps = int(len(train_dataloader) * num_epoch // args.gradient_accumulation_steps)
        warmup_steps = int(total_steps * args.warmup_ratio)
        scheduler = get_cosine_schedule_with_warmup(optimizer, num_warmup_steps=warmup_steps, num_training_steps=total_steps)

        log_step = 50
        total_loss = 0
        for epoch in tqdm(train_iterator):
            start_time = time.time()
            model.zero_grad()
            for step, batch in tqdm(enumerate(train_dataloader)):
                model.train()
                (
                    input_ids, 
                    input_mask,
                    batch_entity_pos, 
                    batch_sent_pos, 
                    # batch_virtual_pos,
                    graph, 
                    num_mention, 
                    num_entity, 
                    num_sent, 
                    # num_virtual,
                    labels, hts
                ) = batch
                # print(batch)


                # print(f'Befor graph {graph}')
                graph_first, features_first = augmentation(graph, input_ids, 0.0, 0.4)
                graph_second, features_second = augmentation(graph, input_ids, 0.1, 0.2)

                inputs = {
                    'input_ids': input_ids.to(args.device),
                    'features_first' : features_first.to(args.device),
                    'features_second' : features_second.to(args.device),
                    'attention_mask': input_mask.to(args.device),
                    'entity_pos': batch_entity_pos,
                    'sent_pos': batch_sent_pos,
                    
                    # 'virtual_pos': batch_virtual_pos,
                    'graph': graph.to(args.device),
                    'graph_first': graph_first.to(args.device),
                    'graph_second': graph_second.to(args.device),
                    'num_mention': num_mention,
                    'num_entity': num_entity,
                    'num_sent': num_sent,
                    # 'num_virtual': num_virtual,
                    'labels': labels,
                    'hts': hts,
                }

                outputs = model(**inputs)
                loss = outputs[0] / args.gradient_accumulation_steps
                loss.backward()
                total_loss += loss.item()
                if step % args.gradient_accumulation_steps == 0:
                    if args.max_grad_norm > 0:
                        torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)

                    optimizer.step()
                    scheduler.step()
                    model.zero_grad()
                    num_steps += 1
                    
                    if num_steps % log_step == 0:
                        cur_loss = total_loss / log_step
                        elapsed = time.time() - start_time

                        logging(
                           '| epoch {:2d} | step {:4d} | min/b {:5.2f} | lr {} | train loss {:5.3f}'.format(
                               epoch, num_steps, elapsed / 60, scheduler.get_lr(), cur_loss * 1000))

                        total_loss = 0
                        start_time = time.time()

                if (step + 1) == len(train_dataloader) - 1 or (args.evaluation_steps > 0 and num_steps % args.evaluation_steps == 0 and step % args.gradient_accumulation_steps == 0):
                    eval_start_time = time.time()
                    dev_score, dev_output = evaluate(args, model, dev_features, tag="dev")
                    test_score, test_output = evaluate(args, model, test_features, tag="test")
                    logging(
                        '| epoch {:3d} | time: {:5.2f}s | dev_output:{} | test_output:{}'.format(epoch, time.time() - eval_start_time,
                                                                                dev_output, test_output))

        return best_score
     

    extract_layer = ["extractor", "bilinear"]
    bert_layer = ['bert_model']
    grace_layer = ['gnn']
  
    optimizer_grouped_parameters = [
        {"params": [p for n, p in model.named_parameters() if any(nd in n for nd in bert_layer)], "lr": args.bert_lr},
        {"params": [p for n, p in model.named_parameters() if any(nd in n for nd in extract_layer)], "lr": 1e-4},
        {"params": [p for n, p in model.named_parameters() if any(nd in n for nd in grace_layer)], "lr": 1e-3},
        {"params": [p for n, p in model.named_parameters() if not any(nd in n for nd in extract_layer + bert_layer + grace_layer)]},
    ]

    optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
    num_steps = 0
    set_seed(args)
    model.zero_grad()
    best_score = finetune(train_features, optimizer, args.num_train_epochs, num_steps)
    print(best_score)
    return best_score
    

def evaluate(args, model, features, tag='test'):
    dataloader = DataLoader(features, batch_size=args.test_batch_size, shuffle=False, collate_fn=collate_fn, drop_last=False)

    preds, golds = [], []
    for batch in tqdm(dataloader):
        model.eval()
        (
            input_ids, 
            input_mask,
            batch_entity_pos, 
            batch_sent_pos, 
            # batch_virtual_pos,
            graph, 
            num_mention, 
            num_entity, 
            num_sent, 
            # num_virtual,
            labels, 
            hts
        ) = batch

        graph_first, features_first = augmentation(graph, input_ids, 0.2, 0.4)
        graph_second, features_second = augmentation(graph, input_ids, 0.3, 0.4)
        inputs = {
            'input_ids': input_ids.to(args.device),
            'features_first' : features_first.to(args.device),
            'features_second' : features_second.to(args.device),
            'attention_mask': input_mask.to(args.device),
            'entity_pos': batch_entity_pos,
            'sent_pos': batch_sent_pos,
            
            # 'virtual_pos': batch_virtual_pos,
            'graph': graph.to(args.device),
            'graph_first': graph_first.to(args.device),
            'graph_second': graph_second.to(args.device),
            'num_mention': num_mention,
            'num_entity': num_entity,
            'num_sent': num_sent,
            # 'num_virtual': num_virtual,
            'labels': labels,
            'hts': hts,
        }


        with torch.no_grad():
            output = model(**inputs)
            loss = output[0]
            pred = output[1].cpu().numpy()
            pred[np.isnan(pred)] = 0
            preds.append(pred)
            golds.append(np.concatenate([np.array(label, np.float32) for label in labels], axis=0))
         

    preds = np.concatenate(preds, axis=0).astype(np.float32)
    golds = np.concatenate(golds, axis=0).astype(np.float32)
    tp = ((preds[:, 1] == 1) & (golds[:, 1] == 1)).astype(np.float32).sum()
    tn = ((golds[:, 1] == 1) & (preds[:, 1] != 1)).astype(np.float32).sum()
    fp = ((preds[:, 1] == 1) & (golds[:, 1] != 1)).astype(np.float32).sum()
    precision = tp / (tp + fp + 1e-5)
    recall = tp / (tp + tn + 1e-5)
    f1 = 2 * precision * recall / (precision + recall + 1e-5)
    output = {
        tag + "_F1": f1 * 100,
        tag + "_P": precision * 100,
        tag + "_R": recall * 100
    }
    return f1, output


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--data_dir", default='./dataset/cdr', type=str)
    parser.add_argument("--transformer_type", default='', type=str)
    parser.add_argument("--model_name", default='', type=str)

    parser.add_argument("--train_file", default='', type=str)
    parser.add_argument("--dev_file", default='', type=str)
    parser.add_argument("--test_file", default='', type=str)
    parser.add_argument("--load_path", default="", type=str)

    parser.add_argument("--gnn_config_file", default="config_file/gnn_config.json", type=str,
                        help="Config gnn model")

    parser.add_argument("--config_name", default="", type=str,
                        help="Pretrained config name or path if not the same as model_name")

    parser.add_argument("--tokenizer_name", default="", type=str,
                        help="Pretrained tokenizer name or path if not the same as model_name")

    parser.add_argument("--max_seq_length", default=1024, type=int,
                        help="The maximum total input sequence length after tokenization. Sequences longer "
                             "than this will be truncated, sequences shorter will be padded.")
    # parser.add_argument("--evaluation_steps", default=400, type=int)

    parser.add_argument("--train_batch_size", default=4, type=int,
                        help="Batch size for training.")

    parser.add_argument("--test_batch_size", default=8, type=int,
                        help="Batch size for testing.")

    parser.add_argument("--gradient_accumulation_steps", default=1, type=int,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")

    parser.add_argument("--num_labels", default=1, type=int,
                        help="Max number of labels in the prediction.")

    parser.add_argument("--learning_rate", default=2e-5, type=float,
                        help="The initial learning rate for Adam.")

    parser.add_argument("--adam_epsilon", default=1e-6, type=float,
                        help="Epsilon for Adam optimizer.")
    parser.add_argument("--max_grad_norm", default=1.0, type=float,
                        help="Max gradient norm.")
    parser.add_argument("--warmup_ratio", default=0.06, type=float,
                        help="Warm up ratio for Adam.")
    parser.add_argument("--num_train_epochs", default=30.0, type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--evaluation_steps", default=-1, type=int,
                        help="Number of training steps between evaluations.")
    parser.add_argument("--seed", type=int, default=111,
                        help="random seed for initialization.")
    parser.add_argument("--num_class", type=int, default=2,
                        help="Number of relation types in collate.")

    parser.add_argument("--unet_in_dim", type=int, default=3,
                        help="unet_in_dim.")
    parser.add_argument("--unet_out_dim", type=int, default=256,
                        help="unet_out_dim.")
    parser.add_argument("--down_dim", type=int, default=256,
                        help="down_dim.")
    parser.add_argument("--channel_type", type=str, default='context-based',
                        help="unet_out_dim.")
    parser.add_argument("--log_dir", type=str, default='',
                        help="log.")
    parser.add_argument("--bert_lr", default=5e-5, type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--max_height", type=int, default=42,
                        help="log.")

    parser.add_argument("--gnn_num_layer", type=int, default=2)
    parser.add_argument("--gnn_node_embedding", type=int, default=50)
    parser.add_argument("--tau", type=float, default=0.4)
    parser.add_argument('--save_path', type=str, default='output')

    args = parser.parse_args()
    # Setup device for pytorch
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    args.device = device

    # Using SciBert
    tokenizer = AutoTokenizer.from_pretrained(args.model_name)
    tokenizer.add_special_tokens({
        'additional_special_tokens': [
            '[ENTITY]',
            '[SENT]',
            '[/ENTITY]',
            '[/SENT]'
        ]
    })

    reader   = read_cdr if "cdr" in args.data_dir else read_gda

    # config collate path
    train_file = os.path.join(args.data_dir, args.train_file)
    dev_file = os.path.join(args.data_dir, args.dev_file)
    test_file = os.path.join(args.data_dir, args.test_file)
    train_features = reader(file_in=train_file, save_file="train.cache", tokenizer=tokenizer, max_seq_length=args.max_seq_length)
    dev_features = reader(file_in=dev_file, save_file="dev.cache", tokenizer=tokenizer, max_seq_length=args.max_seq_length)
    test_features = reader(file_in=test_file, save_file="test.cache",tokenizer=tokenizer, max_seq_length=args.max_seq_length)

    bert_config = AutoConfig.from_pretrained(
        pretrained_model_name_or_path=args.model_name,
        num_labels=args.num_class
    )

    bert_config.cls_token_id = tokenizer.cls_token_id
    bert_config.sep_token_id = tokenizer.sep_token_id
    bert_config.transformer_type = args.transformer_type
    

    bert_model = AutoModel.from_pretrained(
        pretrained_model_name_or_path=args.model_name,
        config=bert_config
    )
    bert_model.resize_token_embeddings(len(tokenizer))

    set_seed(args)
    model = DocREModel(bert_config, args, bert_model)
    
    model.to(device)

    train_features.extend(dev_features)
    train(args, model, train_features, dev_features, test_features)

if __name__ == '__main__':
    torch.cuda.empty_cache()
    main()