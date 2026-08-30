// Native APKG block-begin LOP.
//
// This node deliberately combines two responsibilities that cannot be
// combined in a Python HDA: USD dependency composition and participation as a
// native Solaris context-options block boundary.

#define __TBB_show_deprecation_message_atomic_H
#define __TBB_show_deprecation_message_task_H

#include "LOP_BMFXAPKGBlockBegin.h"

#include <HUSD/HUSD_EditLayers.h>
#include <LOP/LOP_Error.h>
#include <OP/OP_Operator.h>
#include <OP/OP_OperatorTable.h>
#include <PRM/PRM_Include.h>
#include <UT/UT_DSOVersion.h>
#include <UT/UT_String.h>
#include <UT/UT_StringArray.h>

namespace {

PRM_Name path_list_name("apkg_sublayer_paths", "APKG Sublayer Paths");
PRM_Default path_list_default(0, "");

} // namespace

void
newLopOperator(OP_OperatorTable *table)
{
    LOP_BMFXAPKGBlockBegin::templates[0].setInvisible(true);
    OP_Operator *begin_operator = new OP_Operator(
        "bmfx_apkg_block_begin",
        "BMFX APKG Begin",
        LOP_BMFXAPKGBlockBegin::constructor,
        LOP_BMFXAPKGBlockBegin::templates,
        static_cast<unsigned>(0),
        static_cast<unsigned>(1));
    begin_operator->setIconName("LOP_begincontextoptionsblock");
    table->addOperator(begin_operator);

    OP_Operator *end_operator = new OP_Operator(
        "bmfx_apkg_block_end",
        "BMFX APKG End",
        LOP_BMFXAPKGBlockEnd::constructor,
        LOP_BMFXAPKGBlockEnd::templates,
        static_cast<unsigned>(1),
        static_cast<unsigned>(1));
    end_operator->setIconName("LOP_editcontextoptions");
    table->addOperator(end_operator);
}

PRM_Template LOP_BMFXAPKGBlockBegin::templates[] = {
    PRM_Template(PRM_STRING, 1, &path_list_name, &path_list_default),
    PRM_Template(),
};

PRM_Template LOP_BMFXAPKGBlockEnd::templates[] = {
    PRM_Template(),
};

OP_Node *
LOP_BMFXAPKGBlockBegin::constructor(
    OP_Network *network, const char *name, OP_Operator *operator_type)
{
    return new LOP_BMFXAPKGBlockBegin(network, name, operator_type);
}

LOP_BMFXAPKGBlockBegin::LOP_BMFXAPKGBlockBegin(
    OP_Network *network, const char *name, OP_Operator *operator_type)
    : LOP_Node(network, name, operator_type)
{
}

LOP_BMFXAPKGBlockBegin::~LOP_BMFXAPKGBlockBegin() = default;

OP_Node *
LOP_BMFXAPKGBlockEnd::constructor(
    OP_Network *network, const char *name, OP_Operator *operator_type)
{
    return new LOP_BMFXAPKGBlockEnd(network, name, operator_type);
}

LOP_BMFXAPKGBlockEnd::LOP_BMFXAPKGBlockEnd(
    OP_Network *network, const char *name, OP_Operator *operator_type)
    : LOP_Node(network, name, operator_type)
{
}

LOP_BMFXAPKGBlockEnd::~LOP_BMFXAPKGBlockEnd() = default;

int
LOP_BMFXAPKGBlockBegin::contextOptionsStackEffect(int input_index) const
{
    return input_index == 0 ? -1 : 0;
}

bool
LOP_BMFXAPKGBlockEnd::showConvexHull() const
{
    return true;
}

int
LOP_BMFXAPKGBlockEnd::contextOptionsStackEffect(int input_index) const
{
    return input_index == 0 ? 1 : 0;
}

OP_ERROR
LOP_BMFXAPKGBlockEnd::cookMyLop(OP_Context &context)
{
    cookModifyInput(context);
    return error();
}

OP_ERROR
LOP_BMFXAPKGBlockBegin::cookMyLop(OP_Context &context)
{
    if (cookModifyInput(context) >= UT_ERROR_FATAL)
        return error();

    UT_String serialized_paths;
    evalString(
        serialized_paths, path_list_name.getToken(), 0, context.getTime());

    UT_StringArray paths;
    serialized_paths.tokenize(paths, "\r\n");

    HUSD_AutoWriteLock write_lock(editableDataHandle());
    HUSD_EditLayers layers(write_lock);
    layers.setAddLayerPosition(0);

    if (!paths.isEmpty())
    {
        UT_Array<HUSD_LayerOffset> offsets;
        offsets.setSize(paths.size());
        if (!layers.addLayers(paths, offsets))
        {
            addError(LOP_FAILED_TO_ADD_LAYER_FILE);
            return error();
        }
    }

    // Department edits authored between Begin and End must be separable from
    // the upstream APKG layers when the package publisher strips dependencies.
    if (!layers.applyLayerBreak())
        addError(LOP_FAILED_TO_APPLY_LAYER_BREAK);

    return error();
}
