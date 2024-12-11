import asyncio
import dataclasses
import json
import typing
from asyncio import Lock
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

import aiofiles
import aiofiles.os
import httpx
import pydantic
from httpx import AsyncClient
from simnet.errors import BadRequest as SimnetBadRequest
from simnet.errors import InvalidCookies
from simnet.models.genshin.chronicle.characters import Character

from core.dependence.assets import AssetsService
from core.plugin import Plugin, handler
from core.services.template.services import TemplateService
from metadata.genshin import AVATAR_DATA, HONEY_DATA
from plugins.tools.genshin import (CharacterDetails, CookiesNotFoundError,
                                   GenshinHelper, PlayerNotFoundError)
from utils.const import DATA_DIR
from utils.log import logger

from .material import FragileGenshinClient, ItemData, MaterialsData, UserOwned

if TYPE_CHECKING:
    from simnet import GenshinClient
    from telegram import Message, Update
    from telegram.ext import ContextTypes

INTERVAL = 1
DATA_FILE_PATH = DATA_DIR.joinpath("daily_material.json").resolve()


class WeeklyRemaining(Plugin):
    """计算周本素材还需要多少"""

    everyday_materials: "MaterialsData" = MaterialsData()
    """
    everyday_materials 储存的是一周中每天能刷的素材 ID
    按照如下形式组织
    ```python
    everyday_materials[周几][国家] = AreaDailyMaterialsData(
      avatar=[角色, 角色, ...],
      avatar_materials=[精通素材, 精通素材, 精通素材],
      weapon=[武器, 武器, ...]
      weapon_materials=[炼武素材, 炼武素材, 炼武素材, 炼武素材],
    )
    ```
    """

    locks: Tuple[Lock, Lock] = (Lock(), Lock())
    """
    Tuple[每日素材缓存锁, 角色武器材料图标锁]
    """

    async def initialize(self):
        """插件在初始化时，会检查一下本地是否缓存了每日素材的数据"""

        async def task_daily():
            async with self.locks[0]:
                logger.info("正在开始获取每日素材缓存")
                await self._refresh_everyday_materials()

        # 当缓存不存在或已过期（大于 3 天）则重新下载
        # TODO(xr1s): 是不是可以改成 21 天？
        if not await aiofiles.os.path.exists(DATA_FILE_PATH):
            asyncio.create_task(task_daily())
        else:
            mtime = await aiofiles.os.path.getmtime(DATA_FILE_PATH)
            mtime = datetime.fromtimestamp(mtime)
            elapsed = datetime.now() - mtime
            if elapsed.days > 3:
                asyncio.create_task(task_daily())

        # 若存在则直接使用缓存
        if await aiofiles.os.path.exists(DATA_FILE_PATH):
            async with aiofiles.open(DATA_FILE_PATH, "rb") as cache:
                try:
                    self.everyday_materials = self.everyday_materials.parse_raw(await cache.read())
                except pydantic.ValidationError:
                    await aiofiles.os.remove(DATA_FILE_PATH)
                    asyncio.create_task(task_daily())

    async def _refresh_everyday_materials(self, retry: int = 5):
        """刷新来自 honey impact 的每日素材表"""
        for attempts in range(1, retry + 1):
            try:
                response = await self.client.get("https://gensh.honeyhunterworld.com/?lang=CHS")
                response.raise_for_status()
            except (HTTPError, SSLZeroReturnError):
                await asyncio.sleep(1)
                if attempts == retry:
                    logger.error("每日素材刷新失败, 请稍后重试")
                    return
                else:
                    logger.warning("每日素材刷新失败, 正在重试第 %d 次", attempts)
                continue
            self.everyday_materials = _parse_honey_impact_source(response.content)
            # 当场缓存到文件
            content = self.everyday_materials.json(ensure_ascii=False, separators=(",", ":"))
            async with aiofiles.open(DATA_FILE_PATH, "w", encoding="utf-8") as file:
                await file.write(content)
            logger.success("每日素材刷新成功")
            return

    def __init__(
        self,
        assets: AssetsService,
        template_service: TemplateService,
        helper: GenshinHelper,
        character_details: CharacterDetails,
    ):
        self.assets_service = assets
        self.template_service = template_service
        self.helper = helper
        self.character_details = character_details
        self.client = AsyncClient()

    async def _get_items_from_user(
        self, user_id: int, uid: Optional[int], offset: Optional[int]
    ) -> Tuple[Optional["GenshinClient"], "UserOwned"]:
        """获取已经绑定的账号的角色、武器信息"""
        user_data = UserOwned()
        try:
            logger.debug("尝试获取已绑定的原神账号")
            client = await self.helper.get_genshin_client(user_id, player_id=uid, offset=offset)
            logger.debug("获取账号数据成功: UID=%s", client.player_id)
            characters = await client.get_genshin_characters(client.player_id)
            for character in characters:
                if character.name == "旅行者":
                    continue
                character_id = str(AVATAR_DATA[str(character.id)]["id"])
                user_data.avatar[character_id] = ItemData(
                    id=character_id,
                    name=typing.cast(str, character.name),
                    rarity=int(typing.cast(str, character.rarity)),
                    level=character.level,
                    constellation=character.constellation,
                    gid=character.id,
                    origin=character,
                    icon="",
                )
        except (PlayerNotFoundError, CookiesNotFoundError):
            self.log_user(user_id, logger.info, "未查询到绑定的账号信息")
        except InvalidCookies:
            self.log_user(user_id, logger.info, "所绑定的账号信息已失效")
        else:
            # 没有异常返回数据
            return client, user_data
        # 有上述异常的， client 会返回 None
        return None, user_data

    @handler.command("weekly_remaining", block=False)
    async def weekly_remaining(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE"):
        user_id = await self.get_real_user_id(update)
        assert user_id is not None
        uid, offset = self.get_real_uid_or_offset(update)
        message = typing.cast("Message", update.effective_message)

        notice = await message.reply_text("收到消息，稍等片刻")
        self.add_delete_message_job(notice, delay=60)

        # 尝试获取用户已绑定的原神账号信息
        client, user_owned = await self._get_items_from_user(user_id, uid, offset)
        assert client is not None
        full_avatar_ids: List[str] = [
            avatar_id for area in self.everyday_materials.weekday(6).values() for avatar_id in area.avatar
        ]
        weekly_material = await _parse_weekly_material()
        char_to_boss: Dict[int, str] = {}
        boss_need: Dict[str, int] = {}
        for material in weekly_material.values():
            for required_by in material.required_by:
                char_to_boss[required_by] = material.dropped_by
            boss_need[material.dropped_by] = 0
        for avatar_id in full_avatar_ids:
            boss = char_to_boss.get(int(avatar_id), "")
            if len(boss) == 0:
                logger.warning("unknown avatar_id %s", avatar_id)
                continue
            avatar = user_owned.avatar.get(avatar_id)
            avatar = avatar or await self._assemble_item_from_honey_data("avatar", avatar_id)
            if avatar is None:
                logger.warning("unknown avatar_id %s", avatar_id)
                continue
            if avatar.origin is None:
                logger.info("user does not have %s", avatar.name)
                boss_need[boss] += 12
                continue
            fragile_client = FragileGenshinClient(client)
            skills = await self._get_skills_data(fragile_client, avatar.origin)
            assert skills is not None
            logger.info("%s 技能 %s", avatar.name, skills)
            boss_need[boss] += _calculate_skills_need_for_999(skills)
        reply_text = ""
        for boss, need in boss_need.items():
            reply_text += f"{boss}： {need}\n"
        await message.reply_text(reply_text)

    async def _get_skills_data(self, client: "FragileGenshinClient", character: Character) -> Optional[List[int]]:
        if client.damaged:
            return None
        detail = None
        try:
            real_client = typing.cast("GenshinClient", client.client)
            detail = await self.character_details.get_character_details(real_client, character)
        except InvalidCookies:
            client.damaged = True
        except SimnetBadRequest as e:
            if e.ret_code == -502002:
                client.damaged = True
            raise
        if detail is None:
            return None
        talents = [t for t in detail.talents if t.type in ["attack", "skill", "burst"]]
        return [t.level for t in talents]

    async def _assemble_item_from_honey_data(self, item_type: str, item_id: str) -> Optional["ItemData"]:
        """用户拥有的角色和武器中找不到数据时，使用 HoneyImpact 的数据组装出基本信息置灰展示"""
        honey_item = HONEY_DATA[item_type].get(item_id)
        if honey_item is None:
            return None
        try:
            icon = await getattr(self.assets_service, item_type)(item_id).icon()
        except KeyError:
            return None
        return ItemData(
            id=item_id,
            name=typing.cast(str, honey_item[1]),
            rarity=typing.cast(int, honey_item[2]),
            icon=icon.as_uri(),
        )


def _calculate_skills_need_for_999(skills: List[int]) -> int:
    assert len(skills) == 3
    need = 0
    for k in range(3):
        if skills[k] < 9:
            need += 2
        if skills[k] < 8:
            need += 1
        if skills[k] < 7:
            need += 1
    return need


@dataclasses.dataclass
class Material:
    name: str
    dropped_by: str
    required_by: List[int]


async def _parse_weekly_material() -> Dict[str, Material]:
    client = httpx.AsyncClient()
    material_response = await client.get("https://gi.yatta.moe/api/v2/chs/material")
    materials = [
        material
        for material in material_response.json()["data"]["items"].values()
        if material["type"] == "characterLevelUpMaterial"
        and material["rank"] == 5
        and material["name"] != "星与火的基石"
    ]

    async def get_material_detail(client: httpx.AsyncClient, material: Dict[str, str]) -> Material:
        material_id = material["id"]
        material_detail = await client.get(f"https://gi.yatta.moe/api/v2/CHS/material/{material_id}")
        material_detail_additions = {}
        try:
            material_detail_additions = material_detail.json()["data"]["additions"]
        except json.decoder.JSONDecodeError:
            print("decode ambr api error")
            print(material_detail.status_code)
            print(material_detail.text)
            raise
        return Material(
            name=material["name"],
            dropped_by=material_detail_additions["droppedBy"][0]["name"],
            required_by=[
                avatar["id"]
                for avatar in material_detail_additions["requiredBy"]["avatar"]
                if not str(avatar).startswith("10000005-")
            ],
        )

    async def hakushin(client: httpx.AsyncClient, character_id: int) -> Tuple[int, str]:
        character_detail = await client.get(f"https://api.hakush.in/gi/data/zh/character/{character_id}.json")
        return (
            character_id,
            character_detail.json()["Materials"]["Talents"][0][8]["Mats"][2]["Name"],
        )

    async with asyncio.Semaphore(5):
        tasks = [get_material_detail(client, material) for material in materials]
        materials = await asyncio.gather(*tasks)
    materials_map = {material.name: material for material in materials}
    materials_map["???"] = Material(name="???", dropped_by="???", required_by=[])

    new_character = await client.get("https://api.hakush.in/gi/new.json")
    new_character_ids: List[int] = new_character.json()["character"]
    async with asyncio.Semaphore(5):
        tasks = [hakushin(client, new_character_id) for new_character_id in new_character_ids]
        new_character_material = await asyncio.gather(*tasks)
    for character_id, material_name in new_character_material:
        if material_name == "星与火的基石":
            continue
        materials_map[material_name].required_by.append(character_id)

    return materials_map
