"""Authenticated local card metadata API, sharing the existing QQ bridge port."""
import json


def register():
    import nonebot
    import outbound
    from fastapi import Header, HTTPException
    from pydantic import BaseModel, ConfigDict, Field
    import qq_share_cards

    app=nonebot.get_driver().server_app
    if getattr(app.state,'qq_card_api_registered',False):return

    class CardRequest(BaseModel):
        model_config=ConfigDict(extra='forbid')
        segments:list[dict]=Field(max_length=64)

    @app.post('/hermes-outbound/cards/parse')
    async def parse(body:CardRequest,authorization:str=Header(default='')):
        outbound.authorize(authorization)
        if len(json.dumps(body.segments,ensure_ascii=False))>qq_share_cards.MAX_RAW_CHARS:
            raise HTTPException(413,'Card input is too large')
        cards=qq_share_cards.parse_segments(body.segments)
        return {'ok':True,'cards':cards,'text':'\n'.join(qq_share_cards.render_cards(cards)),
                'fetched_content':False}

    app.state.qq_card_api_registered=True
