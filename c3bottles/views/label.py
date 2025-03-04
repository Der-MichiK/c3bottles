from base64 import b64encode
from io import BytesIO
from zipfile import ZipFile

import qrcode
from cairosvg import svg2pdf
from flask import Blueprint, Response, abort, render_template, request
from PyPDF2 import PdfReader, PdfWriter
from stringcase import lowercase, snakecase

from c3bottles import app
from c3bottles.model.category import Category
from c3bottles.model.drop_point import DropPoint
from c3bottles.views import needs_visiting

bp = Blueprint("label", __name__)


@bp.route("/label/<int:number>.pdf")
@needs_visiting
def for_dp(number: int):
    dp = DropPoint.query.get(number)
    if dp is None:
        return abort(404)
    return Response(_create_pdf(dp), mimetype="application/pdf")


@bp.route("/label/all.pdf")
@needs_visiting
def all_labels_pdf():
    output = PdfWriter()
    for dp in DropPoint.query.filter(DropPoint.removed == None).all():  # noqa
        output.add_page(PdfReader(BytesIO(_create_pdf(dp))).pages[0])
    f = BytesIO()
    output.write(f)
    return Response(f.getvalue(), mimetype="application/pdf")


@bp.route("/label/all.zip")
@needs_visiting
def all_labels_zip():
    f = BytesIO()
    with ZipFile(f, "w") as z:
        for dp in DropPoint.query.filter(DropPoint.removed == None).all():  # noqa
            output = PdfWriter()
            output.add_page(PdfReader(BytesIO(_create_pdf(dp))).pages[0])
            pdf = BytesIO()
            output.write(pdf)
            z.writestr(f"{dp.number}.pdf", pdf.getvalue())
    return Response(f.getvalue(), mimetype="application/x-zip")


@bp.route("/label/category/<int:number>.pdf")
@needs_visiting
def for_cat(number: int):
    cat = Category.get(number)
    if cat is None:
        return abort(404)
    output = PdfWriter()
    for dp in DropPoint.query.filter(
        DropPoint.category_id == cat.category_id, DropPoint.removed == None  # noqa
    ).all():
        output.add_page(PdfReader(BytesIO(_create_pdf(dp))).pages[0])
    f = BytesIO()
    output.write(f)
    return Response(f.getvalue(), mimetype="application/pdf")


def _create_pdf(dp: DropPoint):
    img = qrcode.make(request.url_root + str(dp.number), border=1)
    f = BytesIO()
    img.save(f)
    b64 = b64encode(f.getvalue()).decode("utf-8")
    label_style = app.config.get("LABEL_STYLE", "default")
    specific_label_style = label_style + "_" + snakecase(lowercase(dp.category.name))
    try:
        return svg2pdf(
            render_template("label/{}.svg".format(specific_label_style), number=dp.number, qr=b64)
        )
    except:  # noqa
        return svg2pdf(
            render_template("label/{}.svg".format(label_style), number=dp.number, qr=b64)
        )
