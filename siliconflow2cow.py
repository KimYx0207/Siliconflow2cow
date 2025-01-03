import os
import re
import json
import time
import requests
import base64
from io import BytesIO
from typing import List, Tuple
from pathvalidate import sanitize_filename
from PIL import Image
from datetime import datetime, timedelta
import threading
import pickle

import plugins
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common.log import logger
from plugins import *
from config import conf


@plugins.register(
    name="Siliconflow2cow",
    desire_priority=90,
    hidden=False,
    desc="A plugin for generating images using various models.",
    version="2.5.8",
    author="Assistant",
)
class Siliconflow2cow(Plugin):
    def __init__(self):
        super().__init__()
        try:
            self.conf = super().load_config()
            if not self.conf:
                raise Exception("配置未找到。")
    
            self.auth_token = self.conf.get("auth_token")
            if not self.auth_token:
                raise Exception("在配置中未找到认证令牌。")
    
            # 初始化其他参数
            self.drawing_prefixes = self.conf.get("drawing_prefixes", ["绘", "draw"])
            self.image_output_dir = self.conf.get("image_output_dir", "./plugins/siliconflow2cow/images")
            self.clean_interval = float(self.conf.get("clean_interval", 3))
            self.clean_check_interval = int(self.conf.get("clean_check_interval", 3600))
            self.chat_api_url = self.conf.get("CHAT_API_URL", "https://api.siliconflow.cn/v1/chat/completions")
            self.chat_model = self.conf.get("CHAT_MODEL")
            if not self.chat_model:
                raise Exception("在配置中未找到 CHAT_MODEL，请检查 config.json 文件。")
            self.enhancer_prompt = self.conf.get("ENHANCER_PROMPT", "")
            self.enhancer_prompt_flux = self.conf.get("ENHANCER_PROMPT_FLUX", "")
            self.default_drawing_model = self.conf.get("default_drawing_model", "schnell")
            self.dev_model_usage_limit = int(self.conf.get("dev_model_usage_limit", 5))  # 每日限制次数
            self.daily_reset_time = self.conf.get("daily_reset_time", "00:00")  # 每日刷新时间
    
            self.user_usage: Dict[str, int] = {}  # 用户使用次数记录
            self.last_reset_date = datetime.now().date()
            # 加载管理员密码
            self.admin_password = self.conf.get("admin_password", "")
            self.admin_users = self.load_admin_users()  # 加载已认证的管理员用户
    
            if not os.path.exists(self.image_output_dir):
                os.makedirs(self.image_output_dir)
    
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
    
            # 启动定时清理任务
            self.schedule_next_run()
    
            logger.info(f"[Siliconflow2cow] 初始化成功，清理间隔设置为 {self.clean_interval} 天，检查间隔为 {self.clean_check_interval} 秒")
        except Exception as e:
            logger.error(f"[Siliconflow2cow] 初始化失败，错误：{e}")
            raise e

            
    def load_admin_users(self):
        admin_users_file = os.path.join(self.image_output_dir, "admin_users.pkl")
        if os.path.exists(admin_users_file):
            with open(admin_users_file, "rb") as f:
                return pickle.load(f)
        else:
            return set()  # 使用集合来存储用户ID
    
    def save_admin_users(self):
        admin_users_file = os.path.join(self.image_output_dir, "admin_users.pkl")
        with open(admin_users_file, "wb") as f:
            pickle.dump(self.admin_users, f)



    def reset_daily_usage(self):
        """每日重置使用次数"""
        now = datetime.now()
        reset_hour, reset_minute = map(int, self.daily_reset_time.split(":"))
        reset_time = now.replace(hour=reset_hour, minute=reset_minute, second=0, microsecond=0)

        if now >= reset_time and self.last_reset_date < now.date():
            self.user_usage.clear()  # 清空所有用户的使用记录
            self.last_reset_date = now.date()
            logger.info(f"[Siliconflow2cow] 已重置用户 dev 模型的使用次数")
            
    def schedule_next_run(self):
        """安排下一次运行"""
        self.timer = threading.Timer(self.clean_check_interval, self.run_clean_task)
        self.timer.start()

    def run_clean_task(self):
        """运行清理任务并安排下一次运行"""
        self.clean_old_images()
        self.schedule_next_run()

    def on_handle_context(self, e_context: EventContext):
        if e_context["context"].type != ContextType.TEXT:
            return
    
        # 检查并重置使用次数
        self.reset_daily_usage()
    
        user_name = e_context["context"]["receiver"]
        content = e_context["context"].content.strip()
    
        # 检查管理员身份
        is_admin = user_name in self.admin_users
    
        # 处理设置管理员密码的命令（只有管理员可以执行）
        if content.startswith("$set_sf_admin_password "):
            if not is_admin:
                reply = Reply(ReplyType.TEXT, "您没有权限执行此操作，只有管理员可以设置管理员密码。")
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                return
    
            args = content[len("$set_sf_admin_password "):].strip()
            if not args:
                reply = Reply(ReplyType.TEXT, "请提供新的管理员密码。用法：$set_sf_admin_password 密码")
            else:
                new_password = args
                self.admin_password = new_password
                # 更新配置文件中的管理员密码
                conf = super().load_config()
                conf['admin_password'] = new_password
                super().save_config(conf)
                reply = Reply(ReplyType.TEXT, "管理员密码已更新。")
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS
            return
    
        # 处理管理员认证命令
        if content.startswith("$sf_admin_password "):
            args = content[len("$sf_admin_password "):].strip()
            if not args:
                reply = Reply(ReplyType.TEXT, "请提供管理员密码。用法：$sf_admin_password 密码")
            else:
                provided_password = args
                if provided_password == self.admin_password:
                    self.admin_users.add(user_name)
                    self.save_admin_users()
                    reply = Reply(ReplyType.TEXT, "管理员认证成功，您现在是管理员。")
                else:
                    reply = Reply(ReplyType.TEXT, "管理员密码错误，认证失败。")
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS
            return
    
        # 处理 clean_all 命令，只有管理员可以执行
        if content.lower() == "clean_all":
            if is_admin:
                reply = self.clean_all_images()
            else:
                reply = Reply(ReplyType.TEXT, "您没有权限执行此操作。")
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS
            return
    
        if not content.startswith(tuple(self.drawing_prefixes)):
            return
    
        logger.debug(f"[Siliconflow2cow] 收到消息: {content}")
    
        try:
            # 移除前缀
            for prefix in self.drawing_prefixes:
                if content.startswith(prefix):
                    content = content[len(prefix):].strip()
                    break
    
            model_key, image_size, clean_prompt = self.parse_user_input(content)
            logger.debug(f"[Siliconflow2cow] 解析后的参数: 模型={model_key}, 尺寸={image_size}, 提示词={clean_prompt}")
    
            # 如果不是管理员，检查使用限制
            if not is_admin:
                # dev模型使用限制
                if model_key == "dev":
                    usage_count = self.user_usage.get(user_name, 0)
                    if usage_count >= self.dev_model_usage_limit:
                        reply = Reply(ReplyType.TEXT, f"您今天使用 dev 模型的次数已达上限 ({self.dev_model_usage_limit} 次)。")
                        e_context["reply"] = reply
                        e_context.action = EventAction.BREAK_PASS
                        return
    
                    # 记录用户使用次数
                    self.user_usage[user_name] = usage_count + 1
    
            # 生成图片
            original_image_url = self.extract_image_url(clean_prompt)
            logger.debug(f"[Siliconflow2cow] 原始提示词中提取的图片URL: {original_image_url}")
    
            enhanced_prompt = self.enhance_prompt(clean_prompt, model_key)
            logger.debug(f"[Siliconflow2cow] 增强后的提示词: {enhanced_prompt}")
    
            image_url = self.generate_image(enhanced_prompt, original_image_url, model_key, image_size)
            logger.debug(f"[Siliconflow2cow] 生成的图片URL: {image_url}")
    
            if image_url:
                image_path = self.download_and_save_image(image_url)
                logger.debug(f"[Siliconflow2cow] 图片已保存到: {image_path}")
    
                with open(image_path, 'rb') as f:
                    image_storage = BytesIO(f.read())
                reply = Reply(ReplyType.IMAGE, image_storage)
            else:
                logger.error("[Siliconflow2cow] 生成图片失败")
                reply = Reply(ReplyType.ERROR, "生成图片失败。")
    
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS
    
        except Exception as e:
            logger.error(f"[Siliconflow2cow] 发生错误: {e}")
            reply = Reply(ReplyType.ERROR, f"发生错误: {str(e)}")
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS




    def parse_user_input(self, content: str) -> Tuple[str, str, str]:
        model_key = self.extract_model_key(content)
        image_size = self.extract_image_size(content, model_key)  # 传入 model_key
        clean_prompt = self.clean_prompt_string(content, model_key)
        logger.debug(f"[Siliconflow2cow] 解析用户输入: 模型={model_key}, 尺寸={image_size}, 清理后的提示词={clean_prompt}")
        return model_key, image_size, clean_prompt


    def enhance_prompt(self, prompt: str, model_key: str) -> str:
        """根据模型选择合适的提示词增强策略，同时进行翻译"""
        # 将提示词在强化过程中翻译
        logger.debug(f"[Siliconflow2cow] 使用的模型名称：{self.chat_model}")
        logger.debug(f"[Siliconflow2cow] 正在处理提示词: {prompt}")

    
        # 根据模型选择使用的增强策略
        if model_key in ["dev", "flux"]:
            try:
                logger.debug(f"[Siliconflow2cow] 模型 {model_key} 使用 ENHANCER_PROMPT_FLUX 进行提示词增强。")
    
                request_data = {
                    "model": self.chat_model,
                    "messages": [
                        {"role": "system", "content": self.enhancer_prompt_flux},
                        {"role": "user", "content": prompt}
                    ]
                }
                logger.debug(f"[Siliconflow2cow] 提示词增强请求体: {json.dumps(request_data, ensure_ascii=False)}")
    
                response = requests.post(
                    self.chat_api_url,
                    headers={
                        "Content-Type": "application/json; charset=utf-8",
                        "Authorization": f"Bearer {self.auth_token}"
                    },
                    json=request_data
                )
                response.raise_for_status()
                enhanced_prompt = response.json()['choices'][0]['message']['content']
                logger.debug(f"[Siliconflow2cow] 提示词增强完成: {enhanced_prompt}")
                return enhanced_prompt
            except requests.exceptions.HTTPError as e:
                if e.response is not None:
                    logger.error(f"[Siliconflow2cow] 提示词增强失败，状态码: {e.response.status_code}，响应内容: {e.response.text}")
                else:
                    logger.error(f"[Siliconflow2cow] 提示词增强失败: {e}")
                return prompt  # 如果增强失败，返回原始提示词
        else:
            # 使用默认的 ENHANCER_PROMPT 进行提示词增强
            try:
                logger.debug(f"[Siliconflow2cow] 正在使用 ENHANCER_PROMPT 进行提示词增强: {prompt}")
    
                request_data = {
                    "model": self.chat_model,
                    "messages": [
                        {"role": "system", "content": self.enhancer_prompt},
                        {"role": "user", "content": prompt}
                    ]
                }
                logger.debug(f"[Siliconflow2cow] 提示词增强请求体: {json.dumps(request_data, ensure_ascii=False)}")
    
                response = requests.post(
                    self.chat_api_url,
                    headers={
                        "Content-Type": "application/json; charset=utf-8",
                        "Authorization": f"Bearer {self.auth_token}"
                    },
                    json=request_data
                )
                response.raise_for_status()
                enhanced_prompt = response.json()['choices'][0]['message']['content']
                logger.debug(f"[Siliconflow2cow] 提示词增强完成: {enhanced_prompt}")
                return enhanced_prompt
            except requests.exceptions.HTTPError as e:
                if e.response is not None:
                    logger.error(f"[Siliconflow2cow] 提示词增强失败，状态码: {e.response.status_code}，响应内容: {e.response.text}")
                else:
                    logger.error(f"[Siliconflow2cow] 提示词增强失败: {e}")
                return prompt  # 如果增强失败，返回原始提示词





    def generate_image(self, prompt: str, original_image_url: str, model_key: str, image_size: str) -> str:
        if original_image_url:
            logger.debug(f"[Siliconflow2cow] 检测到图片URL，使用图生图模式")
            return self.generate_image_by_img(prompt, original_image_url, model_key, image_size)
        else:
            logger.debug(f"[Siliconflow2cow] 未检测到图片URL，使用文生图模式")
            return self.generate_image_by_text(prompt, model_key, image_size)

    def generate_image_by_text(self, prompt: str, model_key: str, image_size: str) -> str:
        url = self.get_url_for_model(model_key)
        logger.debug(f"[Siliconflow2cow] 使用模型URL: {url}")

        width, height = map(int, image_size.split('x'))

        json_body = {
            "prompt": prompt,
            "width": width,
            "height": height
        }

        headers = {
            'Authorization': f"Bearer {self.auth_token}",
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        }
        
        # 只有 fluxdev 需要单独传递 model 字段
        if model_key == "dev":
            json_body["model"] = "black-forest-labs/FLUX.1-dev"
            json_body.update({
                "num_inference_steps": 30,
                "guidance_scale": 3.5
            })

        elif model_key == "schnell":        
            json_body.update({
                "num_inference_steps": 20,
                "guidance_scale": 3.5
            })
        elif model_key == "sd2":
            json_body.update({
                "num_inference_steps": 25,
                "guidance_scale": 6.0
            })
        elif model_key == "sd35": 
            json_body["model"] = "stabilityai/stable-diffusion-3-5-large"
            json_body.update({
                "num_inference_steps": 30,
                "guidance_scale": 4.5
            })
        elif model_key == "sd3":
            json_body.update({
                "num_inference_steps": 30,
                "guidance_scale": 4.5
            })
        elif model_key == "sdt":
            json_body.update({
                "num_inference_steps": 6,
                "guidance_scale": 1.0,
                "cfg_scale": 1.0
            })
        elif model_key == "sdxlt":
            json_body.update({
                "num_inference_steps": 4,
                "guidance_scale": 1.0
            })
        elif model_key == "sdxll":
            json_body.update({
                "num_inference_steps": 4,
                "guidance_scale": 1.0
            })
        else:
            json_body.update({
                "num_inference_steps": 25,
                "guidance_scale": 3.5
            })



        logger.debug(f"[Siliconflow2cow] 发送请求体: {json_body}")
        try:
            response = requests.post(url, headers=headers, json=json_body)
            response.raise_for_status()
            json_response = response.json()
            logger.debug(f"[Siliconflow2cow] API响应: {json_response}")
            return json_response['images'][0]['url']
        except requests.exceptions.RequestException as e:
            if e.response is not None:
                logger.error(f"[Siliconflow2cow] API请求失败，响应内容: {e.response.text}")
            logger.error(f"[Siliconflow2cow] API请求失败: {e}")

            if hasattr(e, 'response') and e.response is not None:
                if e.response.status_code == 400:
                    error_message = e.response.json().get('error', {}).get('message', '未知错误')
                    logger.error(f"[Siliconflow2cow] API错误信息: {error_message}")
                logger.error(f"[Siliconflow2cow] API响应内容: {e.response.text}")
            raise Exception(f"API请求失败: {str(e)}")
        
            logger.debug(f"[Siliconflow2cow] 发送请求体: {json_body}")
            

    def generate_image_by_img(self, prompt: str, image_url: str, model_key: str, image_size: str) -> str:
        url = self.get_img_url_for_model(model_key)
        logger.debug(f"[Siliconflow2cow] 使用图生图模型URL: {url}")
        img_prompt = self.remove_image_urls(prompt)

        base64_image = self.convert_image_to_base64(image_url)

        width, height = map(int, image_size.split('x'))

        json_body = {
            "prompt": img_prompt,
            "image": base64_image,
            "width": width,
            "height": height,
            "batch_size": 1
        }
        
        if model_key == "sdxl":
            json_body.update({
                "num_inference_steps": 30,
                "guidance_scale": 7.0
            })
        elif model_key == "sd2":
            json_body.update({
                "num_inference_steps": 30,
                "guidance_scale": 7.0
            })
        elif model_key == "sdxll":
            json_body.update({
                "num_inference_steps": 4,
                "guidance_scale": 1.0
            })
        elif model_key == "pm":
            json_body.update({
                "style_name": "Photographic (Default)",
                "guidance_scale": 5,
                "style_strengh_radio": 20
            })
        else:
            json_body.update({
                "num_inference_steps": 30,
                "guidance_scale": 7.5
            })

        headers = {
            'Authorization': f"Bearer {self.auth_token}",
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        }

        log_json_body = json_body.copy()
        log_json_body['image'] = '[BASE64_IMAGE_DATA]'
        logger.debug(f"[Siliconflow2cow] 发送图生图请求体: {log_json_body}")

        try:
            response = requests.post(url, headers=headers, json=json_body)
            response.raise_for_status()
            json_response = response.json()
            logger.debug(f"[Siliconflow2cow] API响应: {json_response}")
            return json_response['images'][0]['url']
        except requests.exceptions.RequestException as e:
            logger.error(f"[Siliconflow2cow] API请求失败: {e}")
            if hasattr(e, 'response') and e.response is not None:
                if e.response.status_code == 400:
                    error_message = e.response.json().get('error', {}).get('message', '未知错误')
                    logger.error(f"[Siliconflow2cow] API错误信息: {error_message}")
                logger.error(f"[Siliconflow2cow] API响应内容: {e.response.text}")
            raise Exception(f"API请求失败: {str(e)}")

    def extract_model_key(self, prompt: str) -> str:
        match = re.search(r'--m ?(\S+)', prompt)
        model_key = match.group(1).strip() if match else self.default_drawing_model  # 使用配置中的 default_drawing_model
        logger.debug(f"[Siliconflow2cow] 提取的模型键: {model_key}")
        return model_key

    def extract_image_size(self, prompt: str, model_key: str) -> str:
        # 定义支持的尺寸映射关系
        ratio_map_common = {
            "1:1": "1024x1024",
            "9:16": "1152x2048",
            "16:9": "2048x1152",
            "3:2": "1536x1024",
            "2:3": "1024x2048",
            "4:3": "1536x2048",
        }

        ratio_map_special = {
            "1:1": "1024x1024",
            "9:16": "576x1024",
            "16:9": "1024x576",
            "3:2": "768x512",
            "2:3": "512x1024",
            "4:3": "768x1024",
        }

        # 根据模型选择对应的尺寸映射
        if model_key in ["sd35", "FLUX.1-schnell", "Pro-FLUX.1-schnell", "FLUX.1-dev"]:
            ratio_map = ratio_map_special
        else:
            ratio_map = ratio_map_common

        match = re.search(r'--ar (\d+:\d+)', prompt)
        if match:
            ratio = match.group(1).strip()
            # 根据映射选择适当的尺寸
            size = ratio_map.get(ratio, "1024x1024")  # 默认使用 1024x1024
        else:
            size = "1024x1024"

        logger.debug(f"[Siliconflow2cow] 提取的图片尺寸: {size}")
        return size


    def clean_prompt_string(self, prompt: str, model_key: str) -> str:
        clean_prompt = re.sub(r' --m ?\S+', '', re.sub(r'--ar \d+:\d+', '', prompt)).strip()
        logger.debug(f"[Siliconflow2cow] 清理后的提示词: {clean_prompt}")
        return clean_prompt

    def extract_image_url(self, text: str) -> str:
        match = re.search(r'(https?://[^\s]+?\.(?:png|jpe?g|gif|bmp|webp|svg|tiff|ico))(?:\s|$)', text, re.IGNORECASE)
        url = match.group(1) if match else None
        logger.debug(f"[Siliconflow2cow] 提取的图片URL: {url}")
        return url

    def convert_image_to_base64(self, image_url: str) -> str:
        logger.debug(f"[Siliconflow2cow] 正在下载图片: {image_url}")
        response = requests.get(image_url)
        if response.status_code != 200:
            logger.error(f"[Siliconflow2cow] 下载图片失败，状态码: {response.status_code}")
            raise Exception('下载图片失败')
        base64_image = f"data:image/webp;base64,{base64.b64encode(response.content).decode('utf-8')}"
        logger.debug("[Siliconflow2cow] 图片已成功转换为base64")
        return base64_image

    def remove_image_urls(self, text: str) -> str:
        cleaned_text = re.sub(r'https?://\S+\.(?:png|jpe?g|gif|bmp|webp|svg|tiff|ico)(?:\s|$)', '', text, flags=re.IGNORECASE)
        logger.debug(f"[Siliconflow2cow] 移除图片URL后的文本: {cleaned_text}")
        return cleaned_text

    def get_url_for_model(self, model_key: str) -> str:
        URL_MAP = {
            "dev": "https://api.siliconflow.cn/v1/image/generations",
            "schnell": "https://api.siliconflow.cn/v1/black-forest-labs/FLUX.1-schnell/text-to-image",
            "sd3": "https://api.siliconflow.cn/v1/stabilityai/stable-diffusion-3-medium/text-to-image",
            "sdxl": "https://api.siliconflow.cn/v1/stabilityai/stable-diffusion-xl-base-1.0/text-to-image",
            "sd2": "https://api.siliconflow.cn/v1/stabilityai/stable-diffusion-2-1/text-to-image",
            "sdt": "https://api.siliconflow.cn/v1/stabilityai/sd-turbo/text-to-image",
            "sdxlt": "https://api.siliconflow.cn/v1/stabilityai/sdxl-turbo/text-to-image",
            "sdxll": "https://api.siliconflow.cn/v1/ByteDance/SDXL-Lightning/text-to-image",
            "sd35": "https://api.siliconflow.cn/v1/images/generations"
        }
        url = URL_MAP.get(model_key, URL_MAP["schnell"])
        logger.debug(f"[Siliconflow2cow] 选择的模型URL: {url}")
        return url

    def get_img_url_for_model(self, model_key: str) -> str:
        IMG_URL_MAP = {
            "sdxl": "https://api.siliconflow.cn/v1/stabilityai/stable-diffusion-xl-base-1.0/image-to-image",
            "sd2": "https://api.siliconflow.cn/v1/stabilityai/stable-diffusion-2-1/image-to-image",
            "sdxll": "https://api.siliconflow.cn/v1/ByteDance/SDXL-Lightning/image-to-image",
            "pm": "https://api.siliconflow.cn/v1/TencentARC/PhotoMaker/image-to-image"
        }
        url = IMG_URL_MAP.get(model_key, IMG_URL_MAP["sdxl"])
        logger.debug(f"[Siliconflow2cow] 选择的图生图模型URL: {url}")
        return url

    RATIO_MAP = {
        "1:1": "1024x1024",
        "1:2": "1024x2048",
        "2:1": "2048x1024",
        "3:2": "1536x1024",
        "2:3": "1024x1536",
        "4:3": "1536x1152",
        "3:4": "1152x1536",
        "16:9": "2048x1152",
        "9:16": "1152x2048"       
    }

    def download_and_save_image(self, image_url: str) -> str:
        logger.debug(f"[Siliconflow2cow] 正在下载并保存图片: {image_url}")
        response = requests.get(image_url)
        if response.status_code != 200:
            logger.error(f"[Siliconflow2cow] 下载图片失败，状态码: {response.status_code}")
            raise Exception('下载图片失败')

        image = Image.open(BytesIO(response.content))

        filename = f"{int(time.time())}.png"
        file_path = os.path.join(self.image_output_dir, filename)

        image.save(file_path, format='PNG')

        logger.info(f"[Siliconflow2cow] 图片已保存到 {file_path}")
        return file_path

    def clean_all_images(self):
        """清理所有图片"""
        logger.debug("[Siliconflow2cow] 开始清理所有图片")
        initial_count = len([name for name in os.listdir(self.image_output_dir) if os.path.isfile(os.path.join(self.image_output_dir, name))])

        for filename in os.listdir(self.image_output_dir):
            file_path = os.path.join(self.image_output_dir, filename)
            if os.path.isfile(file_path):
                os.remove(file_path)
                logger.info(f"[Siliconflow2cow] 已删除图片: {file_path}")

        final_count = len([name for name in os.listdir(self.image_output_dir) if os.path.isfile(os.path.join(self.image_output_dir, name))])

        logger.debug("[Siliconflow2cow] 清理所有图片完成")
        return Reply(ReplyType.TEXT, f"清理完成：已删除 {initial_count - final_count} 张图片，当前目录下还有 {final_count} 张图片。")

    def clean_old_images(self):
        """清理指定天数前的图片"""
        logger.debug(f"[Siliconflow2cow] 开始检查是否需要清理旧图片，清理间隔：{self.clean_interval}天")
        now = datetime.now()
        cleaned_count = 0
        for filename in os.listdir(self.image_output_dir):
            file_path = os.path.join(self.image_output_dir, filename)
            if os.path.isfile(file_path):
                file_time = datetime.fromtimestamp(os.path.getmtime(file_path))
                if now - file_time > timedelta(days=self.clean_interval):
                    os.remove(file_path)
                    cleaned_count += 1
                    logger.info(f"[Siliconflow2cow] 已删除旧图片: {file_path}")
        if cleaned_count > 0:
            logger.debug(f"[Siliconflow2cow] 清理旧图片完成，共清理 {cleaned_count} 张图片")
        else:
            logger.debug("[Siliconflow2cow] 没有需要清理的旧图片")

    def get_help_text(self, **kwargs):
        help_text = "插件使用说明\n"
        help_text += f"1. 使用 {', '.join(self.drawing_prefixes)} 作为命令前缀\n"
        help_text += "2. 在提示词后面添加 '--m' 来选择模型，例如：--m sdxl\n"
        help_text += "3. 使用 '--' 后跟比例来指定图片尺寸，例如：--ar 16:9\n"
        help_text += "4. 如果要进行图生图，直接在提示词中包含图片URL\n"
        help_text += f"示例：{self.drawing_prefixes[0]} 一只可爱的小猫 --m dev --ar 16:9\n\n"
        help_text += "注意：您的提示词将会被AI自动优化以产生更好的结果。\n"
        help_text += "注意：各模型的参数已经过调整以提高图像质量。\n"
        help_text += f"可用的模型：dev,schnell, sd35, sd3, sdxl, sd2, sdt, sdxlt, sdxll\n"
        help_text += f"可用的尺寸比例：{', '.join(self.RATIO_MAP.keys())}\n"
        help_text += f"图片将每{self.clean_interval}天自动清理一次。\n"
        help_text += f"输入 $sf_admin_password 密码 验证管理员，管理员不受每日次数限制，并可执行 '{self.drawing_prefixes[0]}clean_all' 来清理所有图片（警告：这将删除所有已生成的图片）\n"
        return help_text
