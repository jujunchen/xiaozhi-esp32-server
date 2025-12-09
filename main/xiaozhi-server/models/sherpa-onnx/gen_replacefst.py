import pynini
from pynini.lib import utf8, byte
from pynini import cdrewrite

sigma = utf8.VALID_UTF8_CHAR.star

rule1 = pynini.cross('pu3ji4chan2si4', '普济禅寺')
rule10 = pynini.cross('pu3ji4can2si4', '普济禅寺')
rule11 = pynini.cross('pu3ji4can2shi4', '普济禅寺')
rule12 = pynini.cross('pu3ji4chan2shi4', '普济禅寺')
rule23 = pynini.cross('bu3ji4chan2si4', '普济禅寺')
rule24 = pynini.cross('bu3ji4can2si4', '普济禅寺')
rule25 = pynini.cross('bu3ji4can2shi4', '普济禅寺')
rule26 = pynini.cross('bu3jin4chan2shi4', '普济禅寺')
rule27 = pynini.cross('bu3jin4chan2si4', '普济禅寺')
rule28 = pynini.cross('bu3jin4can2si4', '普济禅寺')
rule29 = pynini.cross('bu3jin4can2shi4', '普济禅寺')
rule30 = pynini.cross('bu3jin4chan2shi4', '普济禅寺')
rule2 = pynini.cross('fa2yu2can2si4', '法雨禅寺')
rule20 = pynini.cross('fa2yu2chan2si4', '法雨禅寺')
rule21 = pynini.cross('fa2yu2chan2shi4', '法雨禅寺')
rule3 = pynini.cross('zhou1shan1', '舟山')
rule31 = pynini.cross('zou1san1', '舟山')
rule32 = pynini.cross('zhou1san1', '舟山')
rule33 = pynini.cross('zou1shan1', '舟山')
rule4 = pynini.cross('wan1wan1', '湾湾')
rule5 = pynini.cross('e2er3li4shu4', '鹅耳枥树')



# rule = (rule1 | rule10 | rule11 | rule12 | rule23 | rule24 | rule25 | rule26 | rule27 | rule28 | rule29 | rule30 | rule2 | rule20 | rule21).optimize()
rule = (rule1 | rule10 | rule11 | rule12 | rule23 | rule24 | rule25 | rule26 | rule27 | rule28 | rule29 | rule30 | rule2 | rule20 | rule21 | rule3 | rule31 | rule32 | rule33 | rule4 | rule5).optimize()

rule = cdrewrite(rule, "", "", sigma)

rule.write('models/sherpa-onnx/replace.fst')